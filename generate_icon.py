"""
StreamClipper Pro — High-Resolution Multi-Size Application Icon Generator.

Generates:
  - assets/app_icon.ico (Windows multi-resolution icon: 16, 24, 32, 48, 64, 128, 256)
  - assets/app_icon.png (512x512 high-res master preview)
  - assets/icon.ico     (backward-compatibility copy)
"""

from pathlib import Path
import math
from PIL import Image, ImageDraw, ImageFilter


def create_gradient_canvas(width: int, height: int, start_color: tuple, end_color: tuple) -> Image.Image:
    """Generate a smooth linear 2D diagonal gradient image."""
    base = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(base)
    # Diagonal gradient from top-left to bottom-right
    for y in range(height):
        for x in range(width):
            t = (x / width * 0.6) + (y / height * 0.4)
            t = max(0.0, min(1.0, t))
            r = int(start_color[0] + (end_color[0] - start_color[0]) * t)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * t)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * t)
            a = int(start_color[3] + (end_color[3] - start_color[3]) * t)
            base.putpixel((x, y), (r, g, b, a))
    return base


def create_master_icon(size: int = 1024) -> Image.Image:
    """Render the 1024x1024 master canvas with anti-aliased graphics."""
    # 1. Base transparent canvas
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(size * 0.055)  # ~56px padding
    box = [pad, pad, size - pad, size - pad]
    radius = int(size * 0.22)  # ~225px corner radius squircle

    # 2. Outer Ambient Glow / Shadow behind the squircle
    shadow_mask = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_mask)
    s_draw.rounded_rectangle(
        [pad - 8, pad - 4, size - pad + 8, size - pad + 12],
        radius=radius + 4,
        fill=(99, 102, 241, 70),  # Electric indigo ambient glow
    )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(radius=28))
    img.alpha_composite(shadow_mask)

    # 3. Squircle Background: Obsidian Dark (#12151C)
    # Draw dark body with subtle gradient
    bg_card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg_card)
    bg_draw.rounded_rectangle(
        box,
        radius=radius,
        fill=(18, 21, 28, 255),  # #12151C
        outline=(99, 102, 241, 240),  # Electric Indigo Border #6366F1
        width=int(size * 0.016),  # ~16px border
    )

    # Inner subtle high-tech rim highlight (top edge)
    inner_rim = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    r_draw = ImageDraw.Draw(inner_rim)
    r_draw.rounded_rectangle(
        [pad + 16, pad + 14, size - pad - 16, size - pad - 14],
        radius=radius - 12,
        outline=(255, 255, 255, 22),
        width=4,
    )
    bg_card.alpha_composite(inner_rim)
    img.alpha_composite(bg_card)

    # 4. Central Emblem: Play Button + Clipper Slash
    # We create an emblem mask with a large play triangle, sheared with an energetic 35-degree scissor slice
    emblem_mask = Image.new("L", (size, size), 0)
    e_draw = ImageDraw.Draw(emblem_mask)

    cx = size * 0.49
    cy = size * 0.51
    w = size * 0.46
    h = size * 0.46

    # Play triangle coordinates (pointed right)
    p_left = cx - w * 0.40
    p_right = cx + w * 0.50
    p_top = cy - h * 0.50
    p_bottom = cy + h * 0.50

    # Draw rounded play triangle
    # We define polygons with smooth bevels
    points = [
        (p_left, p_top),
        (p_right, cy),
        (p_left, p_bottom),
    ]
    # Draw thicker base triangle
    e_draw.polygon(points, fill=255)

    # Round the vertex tips by stamping circles at corners
    tip_r = int(size * 0.042)
    e_draw.ellipse([p_left - tip_r, p_top - tip_r, p_left + tip_r, p_top + tip_r], fill=255)
    e_draw.ellipse([p_left - tip_r, p_bottom - tip_r, p_left + tip_r, p_bottom + tip_r], fill=255)
    e_draw.ellipse([p_right - tip_r, cy - tip_r, p_right + tip_r, cy + tip_r], fill=255)
    # Fill between the left circle caps
    e_draw.rectangle([p_left - tip_r, p_top, p_left + tip_r, p_bottom], fill=255)

    # Now carve out a sharp energetic diagonal "Clipper / Scissor" slash cut across the play triangle
    # Cut angle: ~36 degrees, width ~ 38px
    cut_w = int(size * 0.038)
    cut_x1 = cx - size * 0.22
    cut_y1 = cy - size * 0.28
    cut_x2 = cx + size * 0.18
    cut_y2 = cy + size * 0.30

    # Draw angled slash mask to subtract
    dx = cut_x2 - cut_x1
    dy = cut_y2 - cut_y1
    dist = math.hypot(dx, dy)
    nx = -dy / dist * (cut_w / 2.0)
    ny = dx / dist * (cut_w / 2.0)

    slash_poly = [
        (cut_x1 + nx, cut_y1 + ny),
        (cut_x2 + nx, cut_y2 + ny),
        (cut_x2 - nx, cut_y2 - ny),
        (cut_x1 - nx, cut_y1 - ny),
    ]
    e_draw.polygon(slash_poly, fill=0)

    # Also add a stylish film-strip notch / scissor pivot circle cut on the left spine
    pivot_x = p_left + size * 0.04
    pivot_y = cy
    pivot_r = int(size * 0.040)
    e_draw.ellipse([pivot_x - pivot_r, pivot_y - pivot_r, pivot_x + pivot_r, pivot_y + pivot_r], fill=0)

    # 5. Fill Emblem with Vibrant Neon Cyan -> Electric Violet Gradient
    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    # Horizontal & vertical blend from Neon Cyan (#00F0FF) to Electric Violet (#8B5CF6)
    c_start = (0, 240, 255)    # Neon Cyan
    c_mid   = (99, 102, 241)   # Electric Indigo
    c_end   = (168, 85, 247)   # Electric Violet

    for y in range(int(p_top - tip_r), int(p_bottom + tip_r + 1)):
        for x in range(int(p_left - tip_r), int(p_right + tip_r + 1)):
            if 0 <= x < size and 0 <= y < size:
                # Progress from top-left (cyan) to bottom-right (violet)
                u = (x - p_left) / max(1.0, (p_right - p_left))
                v = (y - p_top) / max(1.0, (p_bottom - p_top))
                factor = 0.45 * u + 0.55 * v
                factor = max(0.0, min(1.0, factor))

                if factor < 0.5:
                    t = factor / 0.5
                    r = int(c_start[0] + (c_mid[0] - c_start[0]) * t)
                    g = int(c_start[1] + (c_mid[1] - c_start[1]) * t)
                    b = int(c_start[2] + (c_mid[2] - c_start[2]) * t)
                else:
                    t = (factor - 0.5) / 0.5
                    r = int(c_mid[0] + (c_end[0] - c_mid[0]) * t)
                    g = int(c_mid[1] + (c_end[1] - c_mid[1]) * t)
                    b = int(c_mid[2] + (c_end[2] - c_mid[2]) * t)

                gradient.putpixel((x, y), (r, g, b, 255))

    # Apply emblem mask to gradient
    emblem_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    emblem_layer.paste(gradient, (0, 0), emblem_mask)

    # Add subtle soft glow to the emblem
    glow_mask = emblem_layer.filter(ImageFilter.GaussianBlur(radius=18))
    img.alpha_composite(glow_mask)
    img.alpha_composite(emblem_layer)

    # 6. Live Red Recording Dot in Top-Right Corner
    # Center position inside top-right corner of squircle
    dot_cx = size - pad - int(size * 0.13)
    dot_cy = pad + int(size * 0.13)
    dot_r = int(size * 0.048)  # ~49px radius

    # Outer soft halo
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    h_draw = ImageDraw.Draw(halo)
    h_draw.ellipse(
        [dot_cx - dot_r * 2.2, dot_cy - dot_r * 2.2, dot_cx + dot_r * 2.2, dot_cy + dot_r * 2.2],
        fill=(239, 68, 68, 65),
    )
    halo = halo.filter(ImageFilter.GaussianBlur(radius=20))
    img.alpha_composite(halo)

    # Mid pulse ring
    ring_draw = ImageDraw.Draw(img)
    ring_draw.ellipse(
        [dot_cx - dot_r * 1.35, dot_cy - dot_r * 1.35, dot_cx + dot_r * 1.35, dot_cy + dot_r * 1.35],
        fill=(239, 68, 68, 120),
        outline=(255, 100, 100, 180),
        width=4,
    )

    # Core bright red dot (#EF4444)
    ring_draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=(239, 68, 68, 255),
        outline=(255, 255, 255, 200),
        width=3,
    )

    # Specular white gloss on top of the red dot
    gloss_r = int(dot_r * 0.36)
    ring_draw.ellipse(
        [dot_cx - dot_r * 0.45, dot_cy - dot_r * 0.45, dot_cx - dot_r * 0.45 + gloss_r * 2, dot_cy - dot_r * 0.45 + gloss_r * 2],
        fill=(255, 255, 255, 210),
    )

    return img


def generate_all_icons() -> None:
    app_dir = Path(__file__).resolve().parent
    assets_dir = app_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("[INFO] Rendering 1024x1024 master icon canvas...")
    master_1024 = create_master_icon(1024)

    # Downscale with high quality Lanczos anti-aliasing to 512x512
    master_512 = master_1024.resize((512, 512), Image.Resampling.LANCZOS)

    # Save PNG master preview
    png_path = assets_dir / "app_icon.png"
    master_512.save(png_path, "PNG")
    print(f"[OK] Saved master preview: {png_path} (512x512)")

    # Target Windows ICO sizes:
    # 256, 128, 64, 48, 32, 24, 16
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

    ico_path = assets_dir / "app_icon.ico"
    master_512.save(
        ico_path,
        format="ICO",
        sizes=ico_sizes,
    )
    print(f"[OK] Saved multi-resolution ICO: {ico_path} (sizes: {ico_sizes})")

    # Also overwrite assets/icon.ico for backward compatibility with any scripts referencing the old name
    compat_ico_path = assets_dir / "icon.ico"
    master_512.save(
        compat_ico_path,
        format="ICO",
        sizes=ico_sizes,
    )
    print(f"[OK] Updated compatibility ICO: {compat_ico_path}")


if __name__ == "__main__":
    generate_all_icons()
