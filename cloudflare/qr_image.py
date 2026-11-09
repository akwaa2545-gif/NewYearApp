"""Small monochrome PNG factory for qrcode without Pillow or PyPNG."""
from functools import wraps
import struct
import zlib

from qrcode.image.base import BaseImage


def _chunk(kind, content):
    checksum = zlib.crc32(kind + content) & 0xffffffff
    return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', checksum)


def _color(value):
    colors = {'black': 0, '#000': 0, '#000000': 0, 'white': 1, '#fff': 1, '#ffffff': 1}
    try:
        return colors[str(value).lower()]
    except KeyError:
        raise ValueError('The hosted QR renderer supports black and white only.') from None


class PurePNGImage(BaseImage):
    kind = 'PNG'
    allowed_kinds = ('PNG',)
    needs_drawrect = False

    def new_image(self, **kwargs):
        self.fill_color = _color(kwargs.get('fill_color', 'black'))
        self.back_color = _color(kwargs.get('back_color', 'white'))
        return self

    def drawrect(self, row, col):
        raise NotImplementedError('This renderer reads the completed QR matrix.')

    def _scanlines(self):
        row_bytes = (self.pixel_size + 7) // 8
        padding = -self.pixel_size % 8
        background = (1 << self.pixel_size) - 1 if self.back_color else 0
        quiet_line = b'\x00' + (background << padding).to_bytes(row_bytes, 'big')
        quiet_height = self.border * self.box_size
        yield quiet_line * quiet_height
        module_mask = (1 << self.box_size) - 1
        for modules in self.modules:
            bits = background
            for column, active in enumerate(modules):
                if active:
                    shift = self.pixel_size - (column + self.border + 1) * self.box_size
                    mask = module_mask << shift
                    bits = bits | mask if self.fill_color else bits & ~mask
            line = b'\x00' + (bits << padding).to_bytes(row_bytes, 'big')
            yield line * self.box_size
        yield quiet_line * quiet_height

    def save(self, stream, format=None, kind=None, **kwargs):
        if str(format or kind or self.kind).upper() != 'PNG':
            raise ValueError('The hosted QR renderer supports PNG output only.')
        if kwargs:
            raise TypeError('Unsupported PNG save options: ' + ', '.join(kwargs))
        header = struct.pack('>IIBBBBB', self.pixel_size, self.pixel_size, 1, 0, 0, 0, 0)
        encoded = zlib.compress(b''.join(self._scanlines()), level=6)
        stream.write(b'\x89PNG\r\n\x1a\n')
        stream.write(_chunk(b'IHDR', header))
        stream.write(_chunk(b'IDAT', encoded))
        stream.write(_chunk(b'IEND', b''))


def install():
    """Use the pure PNG factory by default; preserve explicit custom factories."""
    import qrcode

    original = qrcode.QRCode.make_image
    if getattr(original, '_eventdesk_pure_png', False):
        return

    @wraps(original)
    def make_image(self, image_factory=None, **kwargs):
        if image_factory is None and self.image_factory is None:
            image_factory = PurePNGImage
        return original(self, image_factory=image_factory, **kwargs)

    make_image._eventdesk_pure_png = True
    qrcode.QRCode.make_image = make_image
