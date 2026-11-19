"""Independent PSD/PSB validation; never use cached previews as layer proof.

The numerical scope is the file's encoded RGB coordinates.  The low-level
psd-tools compositor is forced to render the layers and returns floats, avoiding
PIL's 8-bit conversion and ICC conversion of conditional difference layers.
Small files use psd-tools. Large files use a separate bounded raw16 parser and
disk-backed compositor; their reports explicitly distinguish the two methods.
This validates the generated 16-bit flat-layer subset, not arbitrary PSD
features or Adobe's native compositor. NumPy working buffers are bounded;
memory-mapped file pages remain subject to the operating system's page cache.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import struct
import shutil
import numpy as np

_Q = 65535.0
_STREAM_PIXELS = 16384
_THIRD_PARTY_MAX_PIXELS = 4_000_000
_BIG_TAGS = {b'Alph', b'FELS', b'FEid', b'FMsk', b'FXid', b'LMsk', b'Layr',
             b'Lr16', b'Lr32', b'Mt16', b'Mt32', b'Mtrn', b'PxSD', b'artd',
             b'cinf', b'extd', b'extn', b'lnk2', b'lnk3', b'lnkE', b'pths'}


class _Reader:
    """Bounded metadata reads; image payloads are sought past, never read here."""
    def __init__(self, file, size):
        self.file, self.size = file, size

    def tell(self):
        return self.file.tell()

    def seek(self, position):
        if not 0 <= position <= self.size:
            raise ValueError('PSD section extends beyond the physical file')
        self.file.seek(position)

    def read(self, size):
        if size < 0 or size > 16*1024*1024 or self.tell()+size > self.size:
            raise ValueError('Invalid or excessively large PSD metadata field')
        data = self.file.read(size)
        if len(data) != size:
            raise ValueError('Truncated PSD metadata')
        return data

    def unpack(self, fmt):
        return struct.unpack('>'+fmt, self.read(struct.calcsize('>'+fmt)))

    def u32(self):
        return self.unpack('I')[0]

    def length(self, version):
        return self.unpack('Q' if version == 2 else 'I')[0]

    def skip_length(self, version=1):
        length = self.length(version)
        self.seek(self.tell()+length)


def _header(path):
    with Path(path).open('rb') as file:
        data = file.read(26)
    if len(data) != 26:
        raise ValueError('Truncated PSD header')
    signature, version, reserved, channels, height, width, depth, mode = struct.unpack('>4sH6sHIIHH', data)
    if signature != b'8BPS' or version not in (1, 2) or reserved != b'\0'*6:
        raise ValueError('Expected PSD version 1 or PSB version 2')
    if not 1 <= width <= (300000 if version == 2 else 30000) or not 1 <= height <= (300000 if version == 2 else 30000):
        raise ValueError('Dimensions exceed the selected PSD/PSB format')
    if channels != 3 or depth != 16 or mode != 3:
        raise ValueError('This validator requires the generated 16-bit RGB, three-channel composite contract')
    return {'version': version, 'width': width, 'height': height, 'depth': depth,
            'format': 'PSB' if version == 2 else 'PSD'}


def _tag_header(reader, version, limit):
    if reader.tell()+12 > limit:
        raise ValueError('Truncated additional-info header')
    signature, key = reader.unpack('4s4s')
    if signature not in (b'8BIM', b'8B64'):
        raise ValueError('Invalid additional-info signature')
    length = reader.length(2 if version == 2 and key in _BIG_TAGS else 1)
    start = reader.tell()
    if start+length > limit:
        raise ValueError('Additional-info payload exceeds its parent')
    return key, start, length


def _parse_raw16(path):
    """Independent parser for our uncompressed flat-layer PSD/PSB subset.

    Large channel sections remain on disk. Offsets and lengths are Python
    integers and retain PSB's 64-bit section and channel sizes.
    """
    path = Path(path)
    result = _header(path)
    width, height, version = result['width'], result['height'], result['version']
    size = path.stat().st_size
    with path.open('rb') as file:
        reader = _Reader(file, size)
        reader.seek(26)
        reader.skip_length()  # color mode
        resource_end = reader.tell()+4
        resource_end += reader.u32()
        if resource_end > size:
            raise ValueError('Image resources exceed file length')
        icc = None
        while reader.tell() < resource_end:
            signature, resource_id = reader.unpack('4sH')
            if signature != b'8BIM':
                raise ValueError('Unsupported image-resource signature')
            name_size = reader.unpack('B')[0]
            reader.read(name_size)
            if (name_size+1) % 2:
                reader.read(1)
            length = reader.u32()
            position = reader.tell()
            if position+length+(length % 2) > resource_end:
                raise ValueError('Image resource extends beyond resource section')
            if resource_id == 1039:
                icc = reader.read(length)
            reader.seek(position+length+(length % 2))
        section_length = reader.length(version)
        section_end = reader.tell()+section_length
        if section_end > size:
            raise ValueError('Layer/mask section exceeds file length')
        reader.skip_length(version)  # standard layer-info; deep data uses Lr16
        reader.skip_length()  # global mask
        layer_payload = None
        while reader.tell() < section_end:
            if section_end-reader.tell() < 12:
                padding = reader.read(section_end-reader.tell())
                if any(padding):
                    raise ValueError('Nonzero layer-section padding')
                break
            key, start, length = _tag_header(reader, version, section_end)
            if key == b'Lr16':
                if layer_payload is not None:
                    raise ValueError('Duplicate Lr16 blocks')
                layer_payload = (start, length)
            next_position = start+length+(-length % 4)
            if next_position > section_end:
                raise ValueError('Additional-info padding exceeds layer section')
            reader.seek(next_position)
        if layer_payload is None:
            raise ValueError('Missing Lr16 layer payload')
        reader.seek(layer_payload[0])
        layer_end = sum(layer_payload)
        count = abs(reader.unpack('h')[0])
        if count < 3:
            raise ValueError('Expected retained original, base and retouch layers')
        layers = []
        for index in range(count):
            if reader.tell()+18 > layer_end:
                raise ValueError('Truncated layer record')
            top, left, bottom, right, channel_count = reader.unpack('4iH')
            if channel_count != 4:
                raise ValueError('Generated layers require RGB plus alpha channels')
            channels = []
            for _ in range(channel_count):
                cid = reader.unpack('h')[0]
                length = reader.length(version)
                channels.append({'id': cid, 'length': length})
            signature, blend = reader.unpack('4s4s')
            if signature != b'8BIM':
                raise ValueError('Invalid layer blend signature')
            flag_offset = reader.tell()+2
            opacity, clipping, flags, filler = reader.unpack('4B')
            extra_length = reader.u32()
            extra_end = reader.tell()+extra_length
            if extra_end > layer_end:
                raise ValueError('Layer extra data exceeds Lr16')
            mask_length = reader.u32()
            reader.seek(reader.tell()+mask_length)
            ranges_length = reader.u32()
            reader.seek(reader.tell()+ranges_length)
            name_size = reader.unpack('B')[0]
            name = reader.read(name_size).decode('macroman', errors='replace')
            reader.read(-(name_size+1) % 4)
            has_unicode = False
            while reader.tell()+12 <= extra_end:
                key, start, length = _tag_header(reader, version, extra_end)
                if key == b'luni':
                    if length < 4:
                        raise ValueError('Invalid Unicode layer name')
                    units = reader.u32()
                    if 4+units*2 > length:
                        raise ValueError('Truncated Unicode layer name')
                    name = reader.read(units*2).decode('utf-16-be')
                    has_unicode = True
                reader.seek(start+length+(-length % 2))
            reader.seek(extra_end)
            if mask_length or ranges_length or clipping:
                raise ValueError('Masks, blend ranges and clipping groups are outside the generated raw-layer contract')
            if set(c['id'] for c in channels) != {0, 1, 2, -1}:
                raise ValueError('Invalid layer channel IDs')
            layers.append({'name': name, 'bbox': (left, top, right, bottom),
                           'visible': not bool(flags & 2), 'opacity': opacity,
                           'blend': blend.decode('ascii'), 'unicode_name': has_unicode,
                           'flag_offset': flag_offset, 'channels': channels})
        expected_channel_length = 2+width*height*2
        for layer in layers:
            for channel in layer['channels']:
                if channel['length'] != expected_channel_length:
                    raise ValueError('Raw16 channel length does not match full-size geometry')
                start = reader.tell()
                if start+channel['length'] > layer_end:
                    raise ValueError('Channel data exceeds the Lr16 payload')
                if reader.unpack('H')[0] != 0:
                    raise ValueError('Streaming validator supports uncompressed raw16 channels only')
                channel['offset'] = reader.tell()
                reader.seek(start+channel['length'])
        if reader.tell() != layer_end:
            raise ValueError('Unexpected bytes within the raw Lr16 payload')
        reader.seek(section_end)
        if reader.unpack('H')[0] != 0:
            raise ValueError('Expected a raw16 merged image')
        merged = reader.tell()
        if merged+width*height*3*2 != size:
            raise ValueError('Merged image length or physical file size is inconsistent')
    result.update({'path': path, 'icc': icc, 'layers': layers,
                   'merged_offset': merged, 'file_bytes': size})
    return result


def _reference_chunk(array, start, end, width):
    # Do not reshape a non-contiguous reference: that could copy an entire image.
    if array.flags.c_contiguous:
        return np.asarray(array.reshape(-1, 3)[start:end], dtype=np.float64)
    result = np.empty((end-start, 3), dtype=np.float64)
    position = start
    while position < end:
        row, col = divmod(position, width)
        take = min(end-position, width-col)
        result[position-start:position-start+take] = array[row, col:col+take]
        position += take
    return result


def _grain_probe_plan(layers):
    """Choose creative layer-opacity probes, not physical grain-strength edits."""
    for i in range(2, len(layers)-1):
        layer = layers[i]
        if '颗粒' in layer['name'] or 'grain' in layer['name'].lower():
            default = layer['opacity']
            result = {'layer_index': i, 'layer_name': layer['name'],
                      'default_opacity_byte': default, 'default_opacity_percent': default/255*100,
                      'boost_at_100_percent': 255/default if default else None,
                      'scope': 'Creative layer-opacity response at the cached upstream state; does not rerender physical grain'}
            if layer.get('encoded_max_deviation', 0.0)*default/255 <= 2/_Q:
                return {**result, 'status': 'not_applicable_neutral_grain_delta'}, {}
            targets = {'decrease': max(0, default//2)}
            if default < 255:
                targets['increase'] = 255
            else:
                result['increase'] = {'status': 'unavailable_default_already_100_percent',
                                      'reason': 'A saturated-boundary/headroom constraint or legacy encoding may require full default opacity'}
            return {**result, 'status': 'completed'}, targets
    return {'status': 'not_applicable_no_named_grain_layer'}, {}


def _record_probe(result, direction, target, maximum):
    result[direction] = {'status': 'changed' if maximum > 1/_Q else 'no_detectable_final_change_at_this_state',
                         'tested_opacity_byte': target, 'tested_opacity_percent': target/255*100,
                         'max_abs_composite_change': maximum}


def validate_psd_streaming(path, reference_rgb=None):
    """Validate every stored pixel using bounded float64 chunks and disk views.

    This compositor is intentionally independent of psd_writer. It is not
    attributed to psd-tools and does not claim third-party whole-file rendering.
    """
    maps = []
    try:
        document = _parse_raw16(path)
        path = document['path']
        width, height = document['width'], document['height']
        layers = document['layers']
        count = width*height
        if reference_rgb is not None:
            reference_rgb = np.asanyarray(reference_rgb)
            if reference_rgb.shape != (height, width, 3):
                raise ValueError('reference_rgb shape does not match the document')
        def plane(offset):
            value = np.memmap(path, mode='r', dtype='>u2', offset=offset, shape=(count,))
            maps.append(value)
            return value
        for layer in layers:
            layer['planes'] = {c['id']: plane(c['offset']) for c in layer['channels']}
            layer['alpha_min_code'], layer['alpha_max_code'] = 65535, 0
            layer['off_ladder_count'] = 0
        merged = [plane(document['merged_offset']+c*count*2) for c in range(3)]
        checks = {'rgb_document': True, 'depth_16': True, 'deep_layer_info_Lr16': True,
                  'icc_rgb': isinstance(document['icc'], bytes) and len(document['icc']) >= 128 and document['icc'][16:20] == b'RGB ' and document['icc'][36:40] == b'acsp',
                  'original_backup_normal': layers[0]['blend'] == 'norm',
                  'base_visible_normal': layers[1]['visible'] and layers[1]['blend'] == 'norm',
                  'top_retouch_normal': layers[-1]['visible'] and layers[-1]['blend'] == 'norm',
                  'all_middle_layers_linear_light': all(l['visible'] and l['blend'] == 'lLit' for l in layers[2:-1]),
                  'all_layers_full_size': all(l['bbox'] == (0, 0, width, height) for l in layers),
                  'source_base_retouch_opaque_setting': all(layers[i]['opacity'] == 255 for i in (0, 1, len(layers)-1)),
                  'all_effect_opacity_positive': all(0 < l['opacity'] <= 255 for l in layers[2:-1]),
                  'all_names_have_unicode_records': all(l['unicode_name'] for l in layers)}
        if not checks['all_layers_full_size'] or not checks['source_base_retouch_opaque_setting'] or not checks['all_effect_opacity_positive']:
            raise ValueError('Full-size generated layers with valid layer-opacity settings are required')
        if any(l['blend'] not in ('norm', 'lLit') for l in layers):
            raise ValueError('Unsupported blend mode in streaming compositor')
        max_difference = difference_sum = max_reference = reference_sum = max_merged_reference = 0.0
        largest_effect, largest_deviation = None, 0.0
        def rgb_chunk(layer, start, end):
            return np.column_stack([layer['planes'][c][start:end] for c in range(3)]).astype(np.float64)/_Q
        def compose(start, end, skip=None, statistics=False, opacity_overrides=None):
            nonlocal largest_effect, largest_deviation
            result = np.zeros((end-start, 3), dtype=np.float64)
            for i, layer in enumerate(layers):
                p = layer['planes']
                if statistics:
                    alpha = p[-1][start:end]
                    low, high = int(alpha.min()), int(alpha.max())
                    layer['alpha_min_code'] = min(layer['alpha_min_code'], low)
                    layer['alpha_max_code'] = max(layer['alpha_max_code'], high)
                    expected = 0 if i == len(layers)-1 else 65535
                    if low != expected or high != expected:
                        raise ValueError('Layer alpha pixels violate the generated opaque/empty contract')
                    layer['off_ladder_count'] += sum(int(np.count_nonzero(p[c][start:end] % 257)) for c in range(3))
                if not layer['visible'] or i == skip or i == len(layers)-1:
                    continue
                source = rgb_chunk(layer, start, end)
                if statistics and 2 <= i < len(layers)-1:
                    deviation = float(np.max(np.abs(source-.5)))
                    layer['encoded_max_deviation'] = max(layer.get('encoded_max_deviation', 0.0), deviation)
                    effective_deviation = deviation*layer['opacity']/255
                    if effective_deviation > largest_deviation:
                        largest_effect, largest_deviation = i, effective_deviation
                if layer['blend'] == 'norm':
                    result[:] = source
                else:
                    # Photoshop/psd-tools Linear Light first clamps burn/dodge,
                    # then interpolates that blend result with the backdrop.
                    opacity = (opacity_overrides or {}).get(i, layer['opacity'])/255
                    source *= 2
                    source -= 1
                    source += result
                    np.clip(source, 0, 1, out=source)
                    result *= 1-opacity
                    result += opacity*source
            return result
        for start in range(0, count, _STREAM_PIXELS):
            end = min(start+_STREAM_PIXELS, count)
            current = compose(start, end, statistics=True)
            merged_chunk = np.column_stack([p[start:end] for p in merged]).astype(np.float64)/_Q
            difference = np.abs(current-merged_chunk)
            max_difference = max(max_difference, float(difference.max()))
            difference_sum += float(difference.sum())
            if reference_rgb is not None:
                reference = _reference_chunk(reference_rgb, start, end, width)
                if not np.isfinite(reference).all():
                    raise ValueError('Reference image contains nonfinite values')
                error = np.abs(current-reference)
                max_reference = max(max_reference, float(error.max()))
                reference_sum += float(error.sum())
                max_merged_reference = max(max_merged_reference, float(np.max(np.abs(merged_chunk-reference))))
        n_delta = len(layers)-3
        tolerance = (n_delta+1)/_Q+2e-6
        metrics = {'layer_vs_merged_max_abs': max_difference,
                   'layer_vs_merged_mean_abs': difference_sum/(count*3),
                   'layer_vs_merged_max_in_8bit_codes': max_difference*255,
                   'acceptance_max_abs': tolerance, 'compositor_dtype': 'float64'}
        checks.update({'retouch_fully_transparent': layers[-1]['alpha_max_code'] == 0,
                       'layer_composite_finite': True, 'layer_composite_shape': True,
                       'layer_composite_opaque': True,
                       'layer_vs_merged_within_quantization_bound': max_difference <= tolerance})
        for i, layer in enumerate(layers[:-1]):
            checks[f'layer_{i}_alpha_opaque'] = layer['alpha_min_code'] == layer['alpha_max_code'] == 65535
        if reference_rgb is not None:
            metrics.update({'layer_vs_reference_max_abs': max_reference,
                            'layer_vs_reference_mean_abs': reference_sum/(count*3),
                            'merged_vs_reference_max_abs': max_merged_reference})
            checks['layer_vs_reference_within_quantization_bound'] = max_reference <= tolerance
            checks['merged_vs_reference_within_16bit_bound'] = max_merged_reference <= .5/_Q+2e-7
        del current, merged_chunk, difference
        if reference_rgb is not None:
            del reference, error
        visibility = {'status': 'not_applicable_all_effect_deltas_neutral'}
        if largest_effect is not None and largest_deviation > 2/_Q:
            change = 0.0
            for start in range(0, count, _STREAM_PIXELS):
                end = min(start+_STREAM_PIXELS, count)
                current = compose(start, end)
                without = compose(start, end, skip=largest_effect)
                change = max(change, float(np.max(np.abs(current-without))))
            visibility = {'status': 'passed' if change > 1/_Q else 'failed',
                          'hidden_layer': layers[largest_effect]['name'], 'max_abs_composite_change': change}
            checks['hiding_effect_changes_true_composite'] = change > 1/_Q
        grain_probe, grain_targets = _grain_probe_plan(layers)
        if grain_targets:
            changes = {key: 0.0 for key in grain_targets}
            for start in range(0, count, _STREAM_PIXELS):
                end = min(start+_STREAM_PIXELS, count)
                current = compose(start, end)
                for direction, target in grain_targets.items():
                    changed = compose(start, end, opacity_overrides={grain_probe['layer_index']: target})
                    changes[direction] = max(changes[direction], float(np.max(np.abs(current-changed))))
            for direction, target in grain_targets.items():
                _record_probe(grain_probe, direction, target, changes[direction])
        summaries = [{'index_bottom_to_top': i, 'name': l['name'], 'visible': l['visible'],
                      'opacity': l['opacity'], 'blend': l['blend'],
                      'default_opacity_percent': l['opacity']/255*100,
                      'boost_at_100_percent': 255/l['opacity'] if l['opacity'] and l['blend'] == 'lLit' else None,
                      'encoded_max_deviation': l.get('encoded_max_deviation', 0.0),
                      'boost_scope': 'opacity ratio under the conditional-difference encoding contract; clipping and upstream edits can change its visual response',
                      'alpha_min': l['alpha_min_code']/_Q, 'alpha_max': l['alpha_max_code']/_Q,
                      'values_off_8bit_ladder_fraction': l['off_ladder_count']/(count*3),
                      'stored_channel_bytes': [c['length']-2 for c in l['channels']]} for i, l in enumerate(layers)]
        return {'status': 'ok' if all(checks.values()) else 'error',
                'validator': 'spektra-independent-streaming-raw16', 'version': '1',
                'path': str(path.resolve()), 'format': document['format'], 'document_version': document['version'],
                'size': [width, height], 'depth': 16, 'layer_count': len(layers),
                'layers_bottom_to_top': summaries,
                'icc_sha256': hashlib.sha256(document['icc']).hexdigest() if document['icc'] else None,
                'render_method': 'Independent raw16 PSD/PSB parser, disk-backed channel views, bounded Linear Light pixel chunks; not a cached preview and not a whole-file psd-tools composite',
                'streaming': {'chunk_pixels': _STREAM_PIXELS, 'reference_disk_backed': isinstance(reference_rgb, np.memmap),
                              'full_channel_copies': False, 'metadata_only_parser': True,
                              'working_memory_scope': 'bounded NumPy pixel buffers; excludes metadata and OS-mapped page cache'},
                'checks': checks, 'metrics': metrics, 'visibility_test': visibility,
                'grain_opacity_adjustment_test': grain_probe,
                'scope': 'Generated raw16 flat-layer PSD/PSB subset in encoded sRGB; no Adobe certification'}
    except Exception as exc:
        return {'status': 'error', 'validator': 'spektra-independent-streaming-raw16',
                'path': str(path), 'code': 'PSD_VALIDATION_FAILED', 'message': str(exc)}
    finally:
        for value in maps:
            value._mmap.close()


def _validate_psd_tools(path, reference_rgb=None):
    """Return JSON-compatible validation facts; never trust the merged preview."""
    try:
        from psd_tools import PSDImage
        from psd_tools.composite import composite
        from psd_tools.constants import BlendMode, ColorMode, Resource, Tag
        path = Path(path)
        psd = PSDImage.open(path)
        layers = list(psd)
        if len(layers) < 3:
            raise ValueError('Expected retained original, base and transparent retouch layers')
        if reference_rgb is not None:
            reference_rgb = np.asarray(reference_rgb, dtype=np.float64)
            if reference_rgb.shape != (psd.height, psd.width, 3) or not np.isfinite(reference_rgb).all():
                raise ValueError('reference_rgb must be a finite HWC RGB array matching the PSD')
        icc = psd.image_resources.get_data(Resource.ICC_PROFILE)
        checks = {
            'rgb_document': psd.color_mode == ColorMode.RGB,
            'depth_16': psd.depth == 16,
            'deep_layer_info_Lr16': Tag.LAYER_16 in psd._record.layer_and_mask_information.tagged_blocks,
            'icc_rgb': isinstance(icc, bytes) and len(icc) >= 128 and icc[16:20] == b'RGB ' and icc[36:40] == b'acsp',
            'original_backup_normal': layers[0].blend_mode == BlendMode.NORMAL,
            'base_visible_normal': layers[1].visible and layers[1].blend_mode == BlendMode.NORMAL,
            'top_retouch_normal': layers[-1].visible and layers[-1].blend_mode == BlendMode.NORMAL,
            'all_middle_layers_linear_light': all(l.visible and l.blend_mode == BlendMode.LINEAR_LIGHT for l in layers[2:-1]),
            'all_layers_full_size': all(l.bbox == (0, 0, psd.width, psd.height) for l in layers),
            'source_base_retouch_opaque_setting': all(layers[i].opacity == 255 for i in (0, 1, len(layers)-1)),
            'all_effect_opacity_positive': all(0 < l.opacity <= 255 for l in layers[2:-1]),
            'all_names_have_unicode_records': all(Tag.UNICODE_LAYER_NAME in l.tagged_blocks for l in layers),
        }
        summaries = []
        largest_effect = None
        largest_deviation = 0.0
        for i, layer in enumerate(layers):
            pixels = layer.numpy()
            if pixels is None or pixels.shape != (psd.height, psd.width, 4):
                raise ValueError(f'Layer {i} does not decode to full RGB+alpha')
            rgb, alpha = pixels[..., :3], pixels[..., 3]
            alpha_min, alpha_max = float(alpha.min()), float(alpha.max())
            if i == len(layers)-1:
                checks['retouch_fully_transparent'] = alpha_max == 0.0
            else:
                checks[f'layer_{i}_alpha_opaque'] = alpha_min == 1.0 and alpha_max == 1.0
            deviation = 0.0
            if 2 <= i < len(layers)-1:
                deviation = float(np.max(np.abs(rgb.astype(np.float64)-0.5)))
                effective = deviation*layer.opacity/255
                if effective > largest_deviation:
                    largest_deviation, largest_effect = effective, layer
            q = np.rint(rgb.astype(np.float64)*_Q).astype(np.uint16)
            summaries.append({'index_bottom_to_top': i, 'name': layer.name,
                              'visible': layer.visible, 'opacity': layer.opacity,
                              'default_opacity_percent': layer.opacity/255*100,
                              'boost_at_100_percent': 255/layer.opacity if layer.opacity and layer.blend_mode == BlendMode.LINEAR_LIGHT else None,
                              'encoded_max_deviation': deviation,
                              'boost_scope': 'opacity ratio under the conditional-difference encoding contract; clipping and upstream edits can change its visual response',
                              'blend': layer.blend_mode.value.decode('ascii'),
                              'alpha_min': alpha_min, 'alpha_max': alpha_max,
                              'values_off_8bit_ladder_fraction': float(np.mean(q % 257 != 0)),
                              'stored_channel_bytes': [len(c.data) for c in layer._channels]})
            del pixels, rgb, alpha, q
        # Direct low-level force composite: cannot take the PSDImage.composite()
        # fast path that returns the stored merged image.
        rendered, shape, alpha = composite(psd, force=True, layer_filter=lambda layer: layer.visible)
        rendered = np.asarray(rendered)
        checks['layer_composite_finite'] = bool(np.isfinite(rendered).all())
        checks['layer_composite_shape'] = rendered.shape == (psd.height, psd.width, 3)
        checks['layer_composite_opaque'] = bool(np.allclose(alpha, 1.0, rtol=0, atol=1e-7))
        merged = psd.numpy('color')
        if merged is None or merged.shape != rendered.shape:
            raise ValueError('Stored merged image has wrong dimensions')
        difference = np.abs(rendered.astype(np.float64)-merged.astype(np.float64))
        n_delta = max(0, len(layers)-3)
        tolerance = (n_delta+1)/_Q + 2e-6  # quantization bound plus float32 compositor arithmetic
        metrics = {'layer_vs_merged_max_abs': float(difference.max()),
                   'layer_vs_merged_mean_abs': float(difference.mean()),
                   'layer_vs_merged_max_in_8bit_codes': float(difference.max()*255),
                   'acceptance_max_abs': tolerance,
                   'compositor_dtype': str(rendered.dtype)}
        checks['layer_vs_merged_within_quantization_bound'] = metrics['layer_vs_merged_max_abs'] <= tolerance
        if reference_rgb is not None:
            e = np.abs(rendered.astype(np.float64)-reference_rgb)
            metrics.update({'layer_vs_reference_max_abs': float(e.max()),
                            'layer_vs_reference_mean_abs': float(e.mean()),
                            'merged_vs_reference_max_abs': float(np.max(np.abs(merged.astype(np.float64)-reference_rgb)))})
            checks['layer_vs_reference_within_quantization_bound'] = metrics['layer_vs_reference_max_abs'] <= tolerance
            checks['merged_vs_reference_within_16bit_bound'] = metrics['merged_vs_reference_max_abs'] <= .5/_Q+2e-7
        visibility = {'status': 'not_applicable_all_effect_deltas_neutral'}
        # At least two file-code amplitudes avoids claiming an effect merely
        # from the unavoidable 16-bit encoding of the neutral value 0.5.
        if largest_effect is not None and largest_deviation > 2/_Q:
            without, _, _ = composite(psd, force=True, layer_filter=lambda layer: layer.visible and layer is not largest_effect)
            change = float(np.max(np.abs(rendered.astype(np.float64)-without.astype(np.float64))))
            visibility = {'status': 'passed' if change > 1/_Q else 'failed',
                          'hidden_layer': largest_effect.name, 'max_abs_composite_change': change}
            checks['hiding_effect_changes_true_composite'] = change > 1/_Q
        grain_probe, grain_targets = _grain_probe_plan(summaries)
        if grain_targets:
            grain_layer = layers[grain_probe['layer_index']]
            original_opacity = grain_layer.opacity
            try:
                for direction, target in grain_targets.items():
                    grain_layer.opacity = target
                    changed, _, _ = composite(psd, force=True, layer_filter=lambda layer: layer.visible)
                    change = float(np.max(np.abs(rendered.astype(np.float64)-changed.astype(np.float64))))
                    _record_probe(grain_probe, direction, target, change)
            finally:
                grain_layer.opacity = original_opacity
        return {'status': 'ok' if all(checks.values()) else 'error',
                'validator': 'psd-tools', 'version': importlib.metadata.version('psd-tools'),
                'path': str(path.resolve()), 'size': [psd.width, psd.height], 'depth': psd.depth,
                'layer_count': len(layers), 'layers_bottom_to_top': summaries,
                'icc_sha256': hashlib.sha256(icc).hexdigest() if isinstance(icc, bytes) else None,
                'render_method': 'psd_tools.composite.composite(force=True), float arrays; no merged-preview shortcut, no PIL/ICC conversion',
                'checks': checks, 'metrics': metrics, 'visibility_test': visibility,
                'grain_opacity_adjustment_test': grain_probe,
                'scope': 'independent open-source PSD parsing and layer compositing; does not certify Adobe Photoshop'}
    except Exception as exc:
        return {'status': 'error', 'validator': 'psd-tools', 'path': str(path),
                'code': 'PSD_VALIDATION_FAILED', 'message': str(exc)}



def validate_psd(path, reference_rgb=None, *, force_streaming=False):
    """Dispatch without allocating full-resolution arrays for large documents.

    Small, ordinary-size files retain independent psd-tools forced compositing.
    PSB axes above that library's 30,000-pixel policy, large pixel counts or
    large physical files use the explicitly labelled streaming implementation.
    """
    try:
        header = _header(path)
        small = (header['width']*header['height'] <= _THIRD_PARTY_MAX_PIXELS and
                 max(header['width'], header['height']) <= 30000 and
                 Path(path).stat().st_size <= 512*1024*1024)
        if force_streaming or not small:
            result = validate_psd_streaming(path, reference_rgb)
        else:
            result = _validate_psd_tools(path, reference_rgb)
            result['format'] = header['format']
            result['document_version'] = header['version']
        result['validation_selection'] = 'forced_streaming' if force_streaming else ('small_file_third_party' if small else 'large_file_streaming')
        return result
    except Exception as exc:
        return {'status': 'error', 'validator': 'spektra-psd-dispatch',
                'path': str(path), 'code': 'PSD_VALIDATION_FAILED', 'message': str(exc)}


def make_hidden_variant(path, output, layer_index):
    """Stream-copy PSD/PSB and patch one visibility byte, keeping merged stale."""
    path, output = Path(path), Path(output)
    if path.resolve() == output.resolve():
        raise ValueError('Variant output must be a new file')
    document = _parse_raw16(path)
    if not 0 <= layer_index < len(document['layers']):
        raise ValueError('Invalid layer index')
    layer = document['layers'][layer_index]
    if not layer['visible']:
        raise ValueError('Selected layer was already hidden')
    offset = layer['flag_offset']
    created = False
    try:
        with path.open('rb') as source, output.open('xb') as destination:
            created = True
            shutil.copyfileobj(source, destination, length=1024*1024)
            source.seek(offset)
            before = source.read(1)[0]
            after = before | 2
            destination.seek(offset)
            destination.write(bytes((after,)))
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return {'path': str(output.resolve()), 'format': document['format'],
            'document_version': document['version'], 'patched_byte_offset': offset,
            'before': before, 'after': after, 'only_one_visibility_byte_changed': True,
            'merged_preview_deliberately_unchanged': True,
            'copy_method': 'bounded 1 MiB stream, no full-file bytearray'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('psd',type=Path)
    parser.add_argument('--reference',type=Path,help='Optional HWC float RGB .npy')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--streaming', action='store_true', help='Force bounded raw16 PSD/PSB validation')
    args=parser.parse_args()
    reference=np.load(args.reference,allow_pickle=False,mmap_mode='r') if args.reference else None
    value=validate_psd(args.psd,reference,force_streaming=args.streaming)
    text=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:args.output.write_text(text,encoding='utf-8')
    print(text)
    return 0 if value['status']=='ok' else 2

if __name__=='__main__':
    raise SystemExit(main())
