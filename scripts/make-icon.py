from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

out = Path(__file__).resolve().parents[1] / "app" / "resources" / "icon.png"
out.parent.mkdir(parents=True, exist_ok=True)
size = 1024
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((36, 36, 988, 988), radius=220, fill=(7, 13, 24, 255))

# Cyan glow behind a simple ghost mark.
glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse((230, 185, 794, 749), fill=(0, 170, 255, 170))
glow = glow.filter(ImageFilter.GaussianBlur(70))
img.alpha_composite(glow)
d = ImageDraw.Draw(img)

# Ghost silhouette with three-tail base.
body = [(260, 680), (260, 470), (270, 360), (320, 265), (405, 205), (512, 185),
        (619, 205), (704, 265), (754, 360), (764, 470), (764, 680),
        (690, 625), (625, 700), (560, 625), (495, 700), (430, 625), (365, 700)]
d.polygon(body, fill=(235, 249, 255, 255))
d.ellipse((380, 390, 455, 465), fill=(7, 28, 44, 255))
d.ellipse((569, 390, 644, 465), fill=(7, 28, 44, 255))

# Location pin cutout.
d.ellipse((452, 490, 572, 610), fill=(0, 142, 255, 255))
d.polygon([(512, 680), (462, 585), (562, 585)], fill=(0, 142, 255, 255))
d.ellipse((492, 530, 532, 570), fill=(235, 249, 255, 255))
img.save(out)
print(out)
