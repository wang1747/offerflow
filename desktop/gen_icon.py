"""生成应用图标 icon.ico：深色圆角底 + 白色四角星（sparkle，AI 智能符号）。

参考 Claude / Codex / Copilot 等现代 AI 应用的图标语言：纯色深底 + 简洁抽象几何符号。
"""

from PIL import Image, ImageDraw

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 深色底（深靛黑渐变，接近纯黑，显高级）
top = (28, 28, 46)     # #1c1c2e
bottom = (14, 14, 22)  # #0e0e16
for y in range(SIZE):
    t = y / SIZE
    r = int(top[0] + (bottom[0] - top[0]) * t)
    g = int(top[1] + (bottom[1] - top[1]) * t)
    b = int(top[2] + (bottom[2] - top[2]) * t)
    d.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))

# 圆角遮罩（大圆角，接近圆形）
mask = Image.new("L", (SIZE, SIZE), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, SIZE, SIZE], radius=74, fill=255)
img.putalpha(mask)

# 白色四角星（sparkle），居中（凹角内缩明显，形成放射状四尖星）
star = [
    (128, 40),    # 上尖
    (150, 106),   # 右上凹
    (216, 128),   # 右尖
    (150, 150),   # 右下凹
    (128, 216),   # 下尖
    (106, 150),   # 左下凹
    (40, 128),    # 左尖
    (106, 106),   # 左上凹
]
d.polygon(star, fill=(255, 255, 255, 255))

img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon.ico generated")

img.resize((256, 256)).save("data/icon_preview_256.png")
print("preview saved")
