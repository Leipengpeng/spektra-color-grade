"""Disk-backed RGB frames and bounded temporary memory for large documents."""
from pathlib import Path
import shutil
import numpy as np
from PIL import Image, ImageCms, ImageOps
import io

MAX_EDGE = 300000

def rows(height, width, samples=262144):
    step = max(1, samples//width)
    for y in range(0, height, step):
        yield slice(y, min(height, y+step))

def mapped(work, name, shape, registry):
    result = np.lib.format.open_memmap(Path(work)/(name+".npy"), mode="w+", dtype=np.float32, shape=shape)
    registry.append(result)
    return result

def output_size(size, max_edge=0, width=None, height=None, resize_mode="keep_aspect"):
    sw, sh = size
    for value in (width, height):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_EDGE):
            raise ValueError("width/height must be integer pixels in [1,300000]")
    if resize_mode not in ("keep_aspect", "stretch"):
        raise ValueError("resize_mode must be keep_aspect or stretch")
    if width is not None or height is not None:
        if max_edge:
            raise ValueError("Use width/height or max_edge, not both")
        if width is None:width = max(1, round(sw*height/sh))
        if height is None:height = max(1, round(sh*width/sw))
        if resize_mode == "keep_aspect" and abs(width*sh/sw-height) > 1:
            raise ValueError("Requested dimensions change aspect ratio. Set one dimension, or resize_mode=stretch explicitly")
        target = width, height
    elif max_edge:
        scale = min(1.0, max_edge/max(sw, sh))
        target = max(1, round(sw*scale)), max(1, round(sh*scale))
    else:target = sw, sh
    if max(target) > MAX_EDGE:
        raise ValueError("Photoshop PSB supports at most 300000 pixels per dimension")
    return target

def inspect(source, max_edge=0, width=None, height=None, resize_mode="keep_aspect"):
    # Explicit local image processing: geometry/resource checks replace Pillow's
    # fixed decompression-bomb pixel cutoff. Never used on unvalidated dimensions.
    previous = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(source) as im:
            orientation = int(im.getexif().get(274, 1))
            size = im.size[::-1] if orientation in (5,6,7,8) else im.size
            target = output_size(size, max_edge, width, height, resize_mode)
            return {"source_size": im.size, "original_oriented_size": list(size), "target_size": list(target), "orientation": orientation}
    finally:Image.MAX_IMAGE_PIXELS = previous

def prepare(source, work, registry, max_edge=0, width=None, height=None, resize_mode="keep_aspect"):
    import OpenImageIO as oiio
    from psd_pipeline import WorkflowError
    try:facts = inspect(source, max_edge, width, height, resize_mode)
    except ValueError as exc:raise WorkflowError("INVALID_GEOMETRY",str(exc),"Use one width/height, preserve the source aspect ratio, or request stretch explicitly.") from exc
    sw, sh = facts["source_size"];tw, th = facts["target_size"]
    needed = sw*sh*12 + tw*th*300 + 128*1024**2
    free = shutil.disk_usage(Path(work)).free
    if needed > free:
        raise WorkflowError("DISK_SPACE_REQUIRED", f"Estimated working space {needed/1024**3:.2f} GiB exceeds available {free/1024**3:.2f} GiB", "Choose another output drive or free disk space; dimensions were not reduced.")
    warning = []
    conversion = {"engine": "untagged encoded sRGB", "transport": "float32"}
    clip_count = 0
    previous = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(source) as im:
            kind, icc, mode = im.format, im.info.get("icc_profile"), im.mode
            if getattr(im,"n_frames",1)>1:warning.append("Multi-image input: only the first image/page is processed.")
            bits = 8
            if kind == "PNG":
                with Path(source).open("rb") as stream:bits = stream.read(25)[24]
            elif kind == "TIFF":
                value = im.tag_v2.get(258,(8,));bits=max((value,) if isinstance(value,int) else value)
            if bits > 16 or mode == "F" or (mode == "I" and bits != 16):
                raise WorkflowError("UNSUPPORTED_INPUT", "Use 8/16-bit SDR input; develop floating HDR/RAW first")
            if mode == "CMYK" and bits > 8:
                raise WorkflowError("UNSUPPORTED_PRECISION", "Develop high-bit-depth CMYK into 16-bit RGB first; an 8-bit intermediate is not substituted")
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else None
            if not icc:warning.append("Untagged CMYK uses Pillow's generic CMYK-to-RGB conversion; colorimetric accuracy is not guaranteed." if mode=="CMYK" else "Untagged input interpreted as encoded sRGB.")
            # CMYK retains the existing explicit ICC conversion path. Ordinary
            # RGB/gray photographs below are decoded scanline by scanline.
            if mode == "CMYK":
                conversion = {"engine": "Pillow ImageCms RGB8" if profile else "Pillow CMYK to RGB8", "transport": "8-bit CMYK input conversion"}
                image = ImageOps.exif_transpose(im)
                image = ImageCms.profileToProfile(image, profile, ImageCms.createProfile("sRGB"), outputMode="RGB") if profile else image.convert("RGB")
                raw = mapped(work, "source", (image.height,image.width,3), registry)
                for part in rows(image.height,image.width):
                    raw[part] = np.asarray(image.crop((0,part.start,image.width,part.stop)),dtype=np.float32)/255
                oriented=raw
            else:
                raw = mapped(work,"source",(sh,sw,3),registry)
                config=oiio.ImageSpec()
                config.attribute("oiio:UnassociatedAlpha",1)
                reader=oiio.ImageInput.open(str(source),config)
                if reader is None:raise WorkflowError("INPUT_DECODE_FAILED",oiio.geterror())
                transform = None
                try:
                    channels=reader.spec().nchannels
                    if profile:
                        from icc_float import FloatICCTransform
                        transform=FloatICCTransform(icc)
                        conversion=dict(transform.metadata)
                    sections = (slice(y,min(sh,y+reader.spec().tile_height)) for y in range(0,sh,reader.spec().tile_height)) if reader.spec().tile_width else rows(sh,sw)
                    for part in sections:
                        if reader.spec().tile_width:
                            a=reader.read_tiles(0,sw,part.start,part.stop,0,1,0,channels,oiio.FLOAT)
                        else:
                            a=reader.read_scanlines(part.start,part.stop,0,0,channels,format=oiio.FLOAT)
                        if a is None:raise WorkflowError("INPUT_DECODE_FAILED",reader.geterror())
                        a=np.asarray(a,dtype=np.float32)
                        if channels in (1,2):rgb=np.repeat(a[...,:1],3,axis=-1)
                        elif channels in (3,4):rgb=a[...,:3]
                        else:raise WorkflowError("UNSUPPORTED_INPUT","Use RGB or grayscale photographic input")
                        if transform:
                            rgb=transform.apply(np.ascontiguousarray(rgb,dtype=np.float32))
                        if channels in (2,4):rgb=rgb*a[...,-1:]+1-a[...,-1:]
                        if not np.isfinite(rgb).all():raise WorkflowError("INPUT_NONFINITE","Nonfinite input")
                        clip_count += int(np.count_nonzero((rgb<0)|(rgb>1)))
                        raw[part]=np.clip(rgb,0,1)
                    if channels in (2,4):warning.append("Transparency composited over white.")
                finally:
                    if transform is not None:transform.close()
                    reader.close()
                orientation=facts["orientation"]
                oriented={2:lambda:raw[:,::-1],3:lambda:raw[::-1,::-1],4:lambda:raw[::-1],
                          5:lambda:raw.transpose(1,0,2),6:lambda:np.rot90(raw,3),
                          7:lambda:raw.transpose(1,0,2)[::-1,::-1],8:lambda:np.rot90(raw)}.get(orientation,lambda:raw)()
    finally:Image.MAX_IMAGE_PIXELS=previous
    result=mapped(work,"original",(th,tw,3),registry)
    if (oriented.shape[1],oriented.shape[0]) == (tw,th):
        for part in rows(th,tw):result[part]=oriented[part]
    else:
        # Only one float channel resides in the resampler at a time. No 8-bit
        # proxy is ever upscaled and substituted for the requested output.
        for c in range(3):
            plane=Image.fromarray(np.asarray(oriented[...,c])).resize((tw,th),Image.Resampling.LANCZOS)
            for part in rows(th,tw):result[part,:,c]=np.clip(np.asarray(plane.crop((0,part.start,tw,part.stop))),0,1)
    result.flush()
    if clip_count:warning.append(f"Input conversion clipped {clip_count/(sw*sh*3):.6%} of channels to SDR sRGB; this conversion is not lossless.")
    return result,{"input_bits":bits,"processing":"disk-backed float32 encoded/linear sRGB; bounded scanline IO",
                   "source_format":kind,"source_mode":mode,"color_conversion":conversion,
                   "input_conversion_clip_channel_fraction":None if mode=="CMYK" else clip_count/(sw*sh*3),
                   "original_oriented_size":facts["original_oriented_size"],"processing_size":[tw,th],
                   "resized":facts["original_oriented_size"]!=[tw,th],"explicit_resize":width is not None or height is not None,
                   "resize_mode":resize_mode,"estimated_working_disk_bytes":needed,"warnings":warning}
