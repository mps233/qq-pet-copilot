#!/usr/bin/env python3
"""图片放大：给仪表盘的背景图补细节（Real-ESRGAN anime x4 ONNX + 混合 + 轻锐化）。

为什么需要它
------------
`dashboard.py` 的 15 款官方「装扮 → 背景」家居背景是从官方 APP 的**卡片预览**里裁的
（见 `qqpet_assets/tools/extract_home_bgs.py`），而卡片的预览图**官方本身就只有
约 350~500px 宽**——实测把手机渲染分辨率放大到 2x/3x/4x 再截，同一块内容在
168/336/504/672px 下并排看**细节完全一样**，说明再截也不会有更多信息。
仪表盘在手机上要按 390 CSS px × DPR 3 ≈ 1170 设备像素显示，504px 的原图必然发虚。

所以这里用超分模型把 504px 补到 2x（1008px），再和"Lanczos 放大"结果混合、
做一次轻度 unsharp，得到一个**在手机上看明显更锐利**的版本。

诚实说明
--------
超分补出来的细节是**模型推断的**（不是官方原图）。`--blend 0.5` 是为
"更锐 vs 更忠实"折中：1.0 = 完全用超分（最锐，星形/木纹这类细节会被模型重画），
0.0 = 只用 Lanczos + 锐化（忠实但提升有限）。默认 0.5。
真·原图只有 app 内部缓存里有（要 root），本机一加 9 Pro 无 root，拿不到。

用法
----
    python3 tools/upscale_images.py static/qp-icons/bg/home-*.jpg --width 1008
    python3 tools/upscale_images.py a.jpg --blend 0 --sharp 60      # 只做忠实放大+锐化
    python3 tools/upscale_images.py a.jpg --dry                    # 只跑不落盘（看耗时/尺寸）

模型：`RealESRGAN_x4plus_anime_4B32F`（xiongjie/lightweight-real-ESRGAN-anime，
5MB、动态输入尺寸，CPU 上 504×1080 → 2016×4320 约 5 秒）。下载到
`resources/models/`（该目录不入库），HF 直连失败自动试 hf-mirror 镜像。
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

BASE = Path(__file__).resolve().parents[1]
MODEL_DIR = BASE / 'resources' / 'models'
MODEL_NAME = 'RealESRGAN_x4plus_anime_4B32F.onnx'
MODEL_URLS = [
    f'https://huggingface.co/xiongjie/lightweight-real-ESRGAN-anime/resolve/main/{MODEL_NAME}',
    f'https://hf-mirror.com/xiongjie/lightweight-real-ESRGAN-anime/resolve/main/{MODEL_NAME}',
]


def ensure_model() -> Path:
    dst = MODEL_DIR / MODEL_NAME
    if dst.is_file() and dst.stat().st_size > 1_000_000:
        return dst
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    last = None
    for url in MODEL_URLS:
        try:
            print(f'[model] 下载 {url}')
            with urllib.request.urlopen(url, timeout=120) as r, open(dst, 'wb') as f:
                f.write(r.read())
            if dst.stat().st_size > 1_000_000:
                print(f'[model] 已就绪 {dst} ({dst.stat().st_size // 1024 // 1024}MB)')
                return dst
        except Exception as e:                                   # noqa: BLE001
            last = e
            print(f'[model] 失败：{e}')
    raise SystemExit(f'模型下载失败：{last}')


def super_resolve(sess, iname, img: Image.Image, tile: int = 512, pad: int = 16) -> Image.Image:
    """整图或分块跑 x4 超分（分块带重叠，避免接缝）。"""
    w, h = img.size
    if max(w, h) <= tile:                                        # 小图直接整张
        x = np.asarray(img, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        y = sess.run(None, {iname: x})[0][0]
        out = np.clip(y.transpose(1, 2, 0), 0, 1)
        return Image.fromarray((out * 255 + 0.5).astype('uint8'))
    out = Image.new('RGB', (w * 4, h * 4))
    step = tile - pad * 2
    for ty in range(0, h, step):
        for tx in range(0, w, step):
            x0, y0 = max(0, tx - pad), max(0, ty - pad)
            x1, y1 = min(w, tx + step + pad), min(h, ty + step + pad)
            sub = img.crop((x0, y0, x1, y1))
            x = np.asarray(sub, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
            y = sess.run(None, {iname: x})[0][0]
            sr = Image.fromarray((np.clip(y.transpose(1, 2, 0), 0, 1) * 255 + 0.5).astype('uint8'))
            # 只贴回非重叠部分
            cx0, cy0 = (tx - x0) * 4, (ty - y0) * 4
            cw, ch = min(step, w - tx) * 4, min(step, h - ty) * 4
            out.paste(sr.crop((cx0, cy0, cx0 + cw, cy0 + ch)), (tx * 4, ty * 4))
    return out


def upscale_file(sess, iname, path: Path, width: int, blend: float, sharp: int,
                 quality: int, dry: bool) -> None:
    img = Image.open(path).convert('RGB')
    target_h = round(img.height * width / img.width)
    base = img.resize((width, target_h), Image.LANCZOS)           # 忠实基准
    if blend > 0:
        sr = super_resolve(sess, iname, img)
        sr = sr.resize((width, target_h), Image.LANCZOS)
        out = Image.blend(base, sr, blend)
    else:
        out = base
    if sharp > 0:
        out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=sharp, threshold=2))
    print(f'{path.name}: {img.size} → {out.size}  blend={blend} sharp={sharp}')
    if not dry:
        out.save(path, 'JPEG', quality=quality, optimize=True)
        print(f'   已写入 {path} ({path.stat().st_size // 1024}KB)')


def main() -> int:
    ap = argparse.ArgumentParser(description='Real-ESRGAN 超分 + 混合 + 锐化')
    ap.add_argument('paths', nargs='+', help='要处理的图片（原地覆盖）')
    ap.add_argument('--width', type=int, default=1008, help='目标宽度（默认 1008 = 504 的 2x）')
    ap.add_argument('--blend', type=float, default=0.5, help='超分占比 0~1（默认 0.5）')
    ap.add_argument('--sharp', type=int, default=80, help='unsharp 强度，0=不锐化（默认 80）')
    ap.add_argument('--quality', type=int, default=88, help='JPEG 质量（默认 88）')
    ap.add_argument('--dry', action='store_true', help='只跑不写盘')
    args = ap.parse_args()

    files = [Path(p) for p in args.paths]
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise SystemExit(f'文件不存在：{missing}')

    sess = iname = None
    if args.blend > 0:
        import onnxruntime as ort                                   # 延迟导入：--blend 0 不需要
        model = ensure_model()
        sess = ort.InferenceSession(str(model), providers=['CPUExecutionProvider'])
        iname = sess.get_inputs()[0].name
    for p in files:
        upscale_file(sess, iname, p, args.width, args.blend, args.sharp, args.quality, args.dry)
    return 0


if __name__ == '__main__':
    sys.exit(main())
