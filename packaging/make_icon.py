"""Draws packaging/sehat_desk.ico (teal square, white cross). The result is committed."""

from pathlib import Path

from PIL import Image, ImageDraw

TEAL = "#008E81"
S = 256

image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill=TEAL)
draw.rounded_rectangle((104, 52, 152, 204), radius=12, fill="white")
draw.rounded_rectangle((52, 104, 204, 152), radius=12, fill="white")
image.save(
    Path(__file__).with_name("sehat_desk.ico"),
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
