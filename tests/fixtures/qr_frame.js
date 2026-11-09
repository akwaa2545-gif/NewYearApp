const fixture = require('./qr_matrix.json');

function qrFrame({width = 640, height = 480, scale = 5, dark = 0, light = 255} = {}) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let i = 0; i < data.length; i += 4) {
    data[i] = data[i + 1] = data[i + 2] = light;
    data[i + 3] = 255;
  }
  const size = fixture.matrix.length * scale;
  const left = Math.floor((width - size) / 2), top = Math.floor((height - size) / 2);
  fixture.matrix.forEach((row, y) => row.forEach((filled, x) => {
    if (!filled) return;
    for (let dy = 0; dy < scale; dy += 1) {
      for (let dx = 0; dx < scale; dx += 1) {
        const i = ((top + y * scale + dy) * width + left + x * scale + dx) * 4;
        data[i] = data[i + 1] = data[i + 2] = dark;
      }
    }
  }));
  return {data, width, height};
}

module.exports = {qrFrame};
