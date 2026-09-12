#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""应用配置预设（config.yaml 就地修改，保留注释）。

用法:
  python tools/apply_preset.py alt --main-name 杨小喵        # 应用小号工具人预设
  python tools/apply_preset.py alt --main-name 杨小喵 --dry  # 只预览不写入
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.presets import apply_alt_preset


def main():
    parser = argparse.ArgumentParser(description='应用配置预设')
    parser.add_argument('preset', choices=['alt'], help='预设名称（alt=小号工具人）')
    parser.add_argument('--main-name', required=True, help='大号的主人昵称或宠物名')
    parser.add_argument('--dry', action='store_true', help='只预览不写入')
    args = parser.parse_args()
    result = apply_alt_preset(args.main_name, dry_run=args.dry)
    print(f'===== 小号工具人预设（大号: {args.main_name}） =====')
    for k, v in result['applied'].items():
        print(f'  {k} = {v!r}')
    if result['rejected']:
        print('被拒绝:')
        for r in result['rejected']:
            print(f'  {r}')
    print(('（预览，未写入）' if result['dry_run'] else '已写入 config.yaml')
          + f'：{len(result["applied"])} 项')


if __name__ == '__main__':
    main()
