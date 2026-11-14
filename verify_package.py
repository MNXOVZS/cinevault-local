# [INPUT]: 迁移包与 package_library 校验。
# [OUTPUT]: 报告文件清单和完整性。
# [POS]: 迁移包检查入口。
# [PROTOCOL]: Update this header on change, then check AGENTS.md
"""Check a CineVault package against its per-file SHA-256 manifest."""
import argparse
from pathlib import Path
import sys
APP = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0,str(APP))
from package_library import verify_package

def main():
    parser=argparse.ArgumentParser(description='检查映库迁移包的文件与校验值。不会解压或改动收藏。')
    parser.add_argument('archive',nargs='?')
    args=parser.parse_args()
    archive=args.archive or input('迁移包 ZIP 的完整路径：').strip().strip('"')
    try:
        result=verify_package(archive)
        print('校验通过。文件数：',len(result['files']),'；模式：',result['profile'])
        print('外部硬盘原片不包含；这是完整性检查，不是来源数字签名。')
    except Exception as exc:
        print('检查失败：',exc)
        raise SystemExit(1)
if __name__=='__main__':
    main()
