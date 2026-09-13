"""Export reviewed Git candidates into a new directory without local data."""
from pathlib import Path
import argparse
import shutil
from github_preflight import audit, git_candidates

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description="生成 GitHub 干净发布目录（不覆盖已有目录）")
    parser.add_argument("--target", type=Path, default=ROOT / "release" / "github")
    args = parser.parse_args()
    target = args.target.resolve()
    release = (ROOT / "release").resolve()
    if target == release or not target.is_relative_to(release):
        raise SystemExit("输出必须是本项目 release/ 下的新子目录。")
    if target.exists():
        raise SystemExit("目标已存在，请用 --target release/github-v2 指定新目录。")
    failures, _ = audit(ROOT)
    if failures:
        raise SystemExit("发布预检查失败：\n" + "\n".join(failures))
    names = git_candidates(ROOT)
    target.mkdir(parents=True)
    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    failures, _ = audit(target, exported=True)
    if failures:
        raise SystemExit("导出检查失败，请勿上传：\n" + "\n".join(failures))
    print(f"发布目录：{target}；{len(names)} 个文件；无 Git 元数据和第三方 Skills。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
