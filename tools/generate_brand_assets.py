import base64
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
README_DIR = ASSET_DIR / "readme"
ASSET_DIR.mkdir(exist_ok=True)
README_DIR.mkdir(parents=True, exist_ok=True)

NAVY = (11, 20, 35)
PANEL = (18, 31, 50)
PANEL_LIGHT = (27, 43, 65)
TEXT = (239, 246, 255)
MUTED = (151, 170, 193)
MINT = (80, 225, 193)
BLUE = (94, 164, 255)
FONT_DIR = Path("C:/Windows/Fonts")
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def font(size, bold=False):
    name = "seguisb.ttf" if bold else "segoeui.ttf"
    path = FONT_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def draw_mark(size):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = size * 0.035
    draw.rounded_rectangle(
        (pad, pad, size - pad, size - pad),
        radius=size * 0.27,
        fill=(17, 35, 55, 255),
        outline=(55, 91, 124, 255),
        width=max(1, size // 90),
    )
    line = max(3, size // 11)
    left = size * 0.31
    top = size * 0.24
    right = size * 0.70
    bottom = size * 0.76
    mid = size * 0.49
    draw.line((left, bottom, left, top), fill=MINT, width=line)
    draw.line((left, top, right * 0.92, top), fill=MINT, width=line)
    draw.arc((left, top, right, mid + size * 0.04), 265, 95, fill=MINT, width=line)
    draw.line((left, mid + size * 0.02, right * 0.82, mid + size * 0.02), fill=MINT, width=line)
    spark = size * 0.075
    sx, sy = size * 0.77, size * 0.25
    draw.line((sx, sy - spark, sx, sy + spark), fill=BLUE, width=max(2, size // 44))
    draw.line((sx - spark, sy, sx + spark, sy), fill=BLUE, width=max(2, size // 44))
    return image


def draw_hero():
    width, height = 1440, 900
    image = Image.new("RGB", (width, height), NAVY)
    pixels = image.load()
    for y in range(height):
        blend = max(0, 1 - abs(y - 500) / 700)
        for x in range(width):
            right_glow = max(0, 1 - abs(x - 1120) / 720) * blend
            pixels[x, y] = (
                int(NAVY[0] + 8 * right_glow),
                int(NAVY[1] + 19 * right_glow),
                int(NAVY[2] + 31 * right_glow),
            )

    draw = ImageDraw.Draw(image)
    mark = draw_mark(58)
    image.paste(mark, (66, 42), mark)
    draw.text((137, 54), "Promptify", font=font(27, True), fill=TEXT)
    draw.text((1040, 61), "WINDOWS  /  OPEN SOURCE  /  BYOK", font=font(15, True), fill=MUTED)

    draw.rounded_rectangle((68, 187, 278, 226), radius=18, fill=(20, 48, 64))
    draw.ellipse((84, 201, 94, 211), fill=MINT)
    draw.text((107, 195), "A QUIETER WAY TO WRITE", font=font(14, True), fill=MINT)
    draw.text((67, 261), "Write like", font=font(69, True), fill=TEXT)
    draw.text((67, 339), "you mean it.", font=font(69, True), fill=TEXT)
    draw.text((72, 443), "Polish, rewrite, or translate selected text", font=font(24), fill=MUTED)
    draw.text((72, 478), "without leaving the app you are already using.", font=font(24), fill=MUTED)

    draw.rounded_rectangle((72, 554, 255, 610), radius=12, fill=MINT)
    draw.text((96, 569), "Get Promptify", font=font(19, True), fill=(9, 31, 39))
    draw.text((72, 633), "Free desktop app  ·  Bring your own API key", font=font(17), fill=MUTED)

    wx, wy, ww, wh = 662, 177, 702, 578
    draw.rounded_rectangle((wx, wy, wx + ww, wy + wh), radius=18,
                           fill=(14, 26, 42), outline=(48, 70, 94), width=2)
    draw.rounded_rectangle((wx, wy, wx + ww, wy + 53), radius=18, fill=(22, 36, 55))
    draw.rectangle((wx, wy + 35, wx + ww, wy + 53), fill=(22, 36, 55))
    for i, color in enumerate(((255, 112, 111), (255, 196, 87), (81, 210, 156))):
        x = wx + 25 + i * 22
        draw.ellipse((x, wy + 20, x + 10, wy + 30), fill=color)
    draw.text((wx + 105, wy + 17), "Message draft  —  Editor", font=font(15), fill=MUTED)

    draw.text((wx + 42, wy + 91), "BEFORE", font=font(13, True), fill=MUTED)
    draw.rounded_rectangle((wx + 36, wy + 119, wx + ww - 36, wy + 267), radius=12,
                           fill=(21, 36, 56), outline=(40, 60, 83))
    draw.text((wx + 61, wy + 146), "hi, i seen your message and i can", font=font(19), fill=(192, 205, 220))
    draw.text((wx + 61, wy + 176), "help you with this tomorrow. let me", font=font(19), fill=(192, 205, 220))
    draw.text((wx + 61, wy + 206), "know if that works for you", font=font(19), fill=(192, 205, 220))

    draw.text((wx + 42, wy + 296), "AFTER  ·  REWRITE (SAME LANGUAGE)", font=font(13, True), fill=MINT)
    draw.rounded_rectangle((wx + 36, wy + 324, wx + ww - 36, wy + 457), radius=12,
                           fill=(17, 42, 49), outline=(42, 105, 99))
    draw.text((wx + 61, wy + 351), "Hi, I saw your message and can help", font=font(19), fill=TEXT)
    draw.text((wx + 61, wy + 382), "you with this tomorrow. Let me know", font=font(19), fill=TEXT)
    draw.text((wx + 61, wy + 413), "if that works for you.", font=font(19), fill=TEXT)
    draw.rounded_rectangle((wx + 37, wy + 493, wx + 180, wy + 535), radius=10, fill=(22, 41, 59))
    draw.text((wx + 58, wy + 505), "Gemini  ·  Ready", font=font(14, True), fill=MINT)
    draw.text((wx + 42, 809), "One click in. Better words out.", font=font(19, True), fill=(172, 190, 211))
    return image


def draw_workflow_frame(progress, logo):
    width, height = 1200, 675
    image = Image.new("RGB", (width, height), NAVY)
    draw = ImageDraw.Draw(image)
    logo = logo.resize((38, 38), Image.Resampling.LANCZOS)
    image.paste(logo, (62, 37), logo)
    draw.text((112, 48), "PROMPTIFY  /  IN THE FLOW", font=font(17, True), fill=MINT)
    draw.text((62, 88), "A better draft, right where you work.", font=font(37, True), fill=TEXT)

    draw.rounded_rectangle((62, 166, 1138, 596), radius=20,
                           fill=(14, 26, 42), outline=(48, 70, 94), width=2)
    draw.rounded_rectangle((62, 166, 1138, 213), radius=20, fill=(22, 36, 55))
    draw.rectangle((62, 194, 1138, 213), fill=(22, 36, 55))
    draw.text((91, 181), "Compose  ·  Draft", font=font(15, True), fill=MUTED)
    draw.text((105, 249), "ORIGINAL", font=font(13, True), fill=MUTED)
    draw.rounded_rectangle((98, 279, 1099, 385), radius=12, fill=(21, 36, 56))
    draw.text((127, 302), "i wanted to ask if we can move the meeting to friday", font=font(22), fill=(194, 207, 222))
    draw.text((127, 338), "because i have some urgent work today", font=font(22), fill=(194, 207, 222))

    output = "Could we move the meeting to Friday? I have urgent work to finish today."
    visible_words = max(0, min(len(output.split()), round(progress * len(output.split()))))
    visible = " ".join(output.split()[:visible_words])
    draw.text((105, 416), "PROMPTIFY  ·  REWRITE", font=font(13, True), fill=MINT)
    draw.rounded_rectangle((98, 447, 1099, 548), radius=12, fill=(17, 42, 49), outline=(42, 105, 99))
    draw.text((127, 480), visible, font=font(22), fill=TEXT)
    if visible_words < len(output.split()):
        cursor_x = 127 + draw.textbbox((0, 0), visible, font=font(22))[2]
        draw.rounded_rectangle((cursor_x + 4, 481, cursor_x + 7, 510), radius=1, fill=MINT)
    percent = int(progress * 100)
    draw.text((100, 579), f"Writing  ·  {percent}%", font=font(14, True), fill=MUTED)
    draw.rounded_rectangle((272, 586, 1098, 594), radius=4, fill=(35, 54, 73))
    draw.rounded_rectangle((272, 586, 272 + int(826 * progress), 594), radius=4, fill=MINT)
    return image


def icon_from_svg():
    svg_root = ET.parse(ROOT / "latest.svg").getroot()
    embedded_image = svg_root.find(f".//{SVG_NAMESPACE}image")
    if embedded_image is None:
        raise ValueError("latest.svg does not contain an embedded image.")

    image_data = embedded_image.get(XLINK_HREF) or embedded_image.get("href")
    if not image_data or not image_data.startswith("data:image/png;base64,"):
        raise ValueError("latest.svg must contain an embedded PNG image.")

    png_bytes = base64.b64decode(image_data.split(",", 1)[1], validate=True)
    with Image.open(BytesIO(png_bytes)) as image:
        image.load()
        return image.convert("RGBA")


def main():
    icon = icon_from_svg()
    icon.save(ASSET_DIR / "promptify-icon.png", optimize=True)
    icon.save(
        ASSET_DIR / "promptify.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    draw_hero().save(README_DIR / "hero.png", optimize=True)
    frames = [draw_workflow_frame(index / 20, icon) for index in range(21)]
    frames += [frames[-1]] * 5
    frames[0].save(
        README_DIR / "workflow.gif",
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        optimize=True,
    )


if __name__ == "__main__":
    main()