#!/bin/sh
# ragkernel 一键安装：装 uv → clone → uv sync → 交棒给 `ragkernel setup`。
#
#   curl -fsSL https://raw.githubusercontent.com/v0id-byte/ragkernel/main/install.sh | sh
#
# 本脚本**完全非交互**——curl|sh 下 stdin 就是脚本本身，任何 read 都会吃掉脚本字节。
# 所有配置交互都在末尾交棒的 `ragkernel setup` 里（它从 /dev/tty 读）。
#
# POSIX sh，不用 bashism：无 [[ ]] / 数组 / local / echo -e / pipefail。

set -eu

REPO="https://github.com/v0id-byte/ragkernel"

DIR="${RAGKERNEL_DIR:-$HOME/ragkernel}"
REF="${RAGKERNEL_REF:-}"
CAD=0
UPDATE=0
NO_SETUP="${RAGKERNEL_NO_SETUP:-}"
PYTHON="3.12"

usage() {
  cat <<'EOF'
用法：install.sh [选项]
  --dir <路径>     安装目录（默认 ~/ragkernel；亦可 RAGKERNEL_DIR=）
  --ref <ref>      指定 branch / tag / commit（亦可 RAGKERNEL_REF=）
  --cad            装可选的原生 CAD extra（重二进制轮子）
  --update         目录已存在时拉取更新（默认不拉——安装≠更新）
  --no-setup       只装环境，不进配置向导（亦可 RAGKERNEL_NO_SETUP=1）
  --python <ver>   Python 版本（默认 3.12）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#*=}"; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --ref=*) REF="${1#*=}"; shift ;;
    --python) PYTHON="$2"; shift 2 ;;
    --python=*) PYTHON="${1#*=}"; shift ;;
    --cad) CAD=1; shift ;;
    --update) UPDATE=1; shift ;;
    --no-setup) NO_SETUP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ 预检

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) die "不支持的系统：$OS（仅 macOS / Linux）" ;;
esac

# torch/embedding/reranker 在 32 位 ARM 没有可用轮子——早失败好过 sync 到一半炸
case "$(uname -m)" in
  x86_64|amd64|arm64|aarch64) ;;
  *) die "不支持的架构：$(uname -m)（需要 x86_64 或 arm64）" ;;
esac

command -v curl >/dev/null 2>&1 || die "缺少 curl，请先安装后重试"
if ! command -v git >/dev/null 2>&1; then
  if [ "$OS" = Darwin ]; then
    die "缺少 git：运行 xcode-select --install 后重试"
  else
    die "缺少 git：用你的包管理器安装（如 apt-get install git）后重试"
  fi
fi

# ------------------------------------------------------------------ uv 引导

if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  say "安装 uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # 官方安装器落在 ~/.local/bin；Homebrew 在 /opt/homebrew/bin。装完再探一次，别只信 PATH
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
  [ -x "$UV" ] || die "uv 安装后仍未找到；请手动安装：https://docs.astral.sh/uv/"
fi
say "uv: $UV"

# ------------------------------------------------------------------ clone / 更新

# 规范化 remote URL 以便比对：去 .git 后缀、ssh→https
normalize() {
  printf '%s' "$1" | sed -e 's,\.git$,,' -e 's,^git@github.com:,https://github.com/,'
}

clone_ref() {
  # 按 ref 类型分流：--branch 不接受裸 commit，commit 要全量+checkout
  if git ls-remote --heads "$REPO" "$REF" 2>/dev/null | grep -q . \
     || git ls-remote --tags "$REPO" "$REF" 2>/dev/null | grep -q .; then
    git clone --depth 1 --branch "$REF" "$REPO" "$DIR"
  else
    say "ref '$REF' 非分支/标签，按 commit 处理（partial clone）"
    git clone --filter=blob:none "$REPO" "$DIR"
    git -C "$DIR" checkout "$REF" || die "未知 ref：$REF"
  fi
}

# 目录不存在，或存在但为空（如 /opt/ragkernel 预建好授权）——都当 clone 处理：git clone 接受空目录
if [ ! -e "$DIR" ] || { [ -d "$DIR" ] && [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; }; then
  say "clone → $DIR"
  if [ -n "$REF" ]; then clone_ref; else git clone --depth 1 "$REPO" "$DIR"; fi
elif [ -d "$DIR/.git" ]; then
  ORIGIN="$(git -C "$DIR" remote get-url origin 2>/dev/null || true)"
  [ "$(normalize "$ORIGIN")" = "$(normalize "$REPO")" ] \
    || die "$DIR 已存在但不是 ragkernel（origin=$ORIGIN）；换个目录：--dir <路径>"
  if [ "$UPDATE" -eq 1 ]; then
    if [ -n "$REF" ]; then
      # 浅克隆要先加深，否则老 commit / 分支新 tip 取不到（checkout 报 reference is not a tree）
      if [ -f "$DIR/.git/shallow" ]; then
        git -C "$DIR" fetch --unshallow --tags origin 2>/dev/null || git -C "$DIR" fetch --tags origin
      else
        git -C "$DIR" fetch --tags --force origin
      fi
      # 分支 ref → 推进到远端 tip（否则 checkout 只停在旧 commit）；tag/commit → 直接 checkout
      if git -C "$DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
        git -C "$DIR" checkout -B "$REF" "origin/$REF"
      else
        git -C "$DIR" checkout "$REF" || die "未知 ref：$REF"
      fi
    elif ! BRANCH="$(git -C "$DIR" symbolic-ref --short -q HEAD)"; then
      say "detached HEAD @ $(git -C "$DIR" describe --tags --always 2>/dev/null)，跳过更新"
    else
      # 默认分支动态探测：fork / 企业 mirror 可能是 master/stable
      DEFAULT_BRANCH="$(git -C "$DIR" remote show origin 2>/dev/null | sed -n '/HEAD branch/s/.*: //p')"
      [ -n "$DEFAULT_BRANCH" ] || DEFAULT_BRANCH=main
      if [ "$BRANCH" != "$DEFAULT_BRANCH" ]; then
        say "当前分支 $BRANCH（默认 $DEFAULT_BRANCH），跳过自动更新"
      elif [ -n "$(git -C "$DIR" status --porcelain)" ]; then
        say "工作区有未提交改动，跳过 pull（仍会 sync）"
      else
        say "更新分支：$BRANCH"
        git -C "$DIR" pull --ff-only
      fi
    fi
  else
    # 安装 ≠ 更新（同 docker pull 与 docker run 分开）：默认原样保留，别把生产悄悄升级
    say "$DIR 已存在；保持当前版本（要更新加 --update）"
  fi
else
  die "$DIR 已存在但不是 git 仓库；换个目录：--dir <路径>"
fi

# ------------------------------------------------------------------ 同步依赖

# CPU torch 由 pyproject 的 [tool.uv.sources] + lockfile 保证，不需要任何环境变量
if [ "$CAD" -eq 1 ]; then
  say "同步依赖（uv sync --python $PYTHON --extra cad）…"
  "$UV" sync --directory "$DIR" --python "$PYTHON" --extra cad
else
  say "同步依赖（uv sync --python $PYTHON）…"
  "$UV" sync --directory "$DIR" --python "$PYTHON"
fi

# ------------------------------------------------------------------ 安装指纹

# 放 .ragkernel/ 而非 data/：这是部署元数据，不是运行数据（data/ 才是备份对象）。绝不写密钥。
COMMIT="$(git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
UVVER="$("$UV" --version 2>/dev/null | awk '{print $2}')"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REF_REC="${REF:-$(git -C "$DIR" symbolic-ref --short -q HEAD 2>/dev/null || echo detached)}"
EXTRAS='[]'; [ "$CAD" -eq 1 ] && EXTRAS='["cad"]'
mkdir -p "$DIR/.ragkernel"
# 重跑保留 installed_at，只更新 updated_at
OLD_INSTALLED=""
[ -f "$DIR/.ragkernel/install.json" ] && OLD_INSTALLED="$(sed -n 's/.*"installed_at" *: *"\([^"]*\)".*/\1/p' "$DIR/.ragkernel/install.json")"
INSTALLED_AT="${OLD_INSTALLED:-$NOW}"
cat > "$DIR/.ragkernel/install.json" <<JSON
{
  "schema_version": 1,
  "installer": "install.sh",
  "installer_version": 1,
  "installed_at": "$INSTALLED_AT",
  "updated_at": "$NOW",
  "ref": "$REF_REC",
  "commit": "$COMMIT",
  "python": "$PYTHON",
  "uv": "$UVVER",
  "extras": $EXTRAS
}
JSON

# ------------------------------------------------------------------ 环境摘要 + 交棒

printf '\nRagKernel bootstrap environment ready.\n\n'
printf '  Location  %s\n' "$DIR"
printf '  Ref       %s (%s)\n' "$REF_REC" "$COMMIT"
printf '  Python    %s\n' "$PYTHON"
printf '  uv        %s\n' "$UVVER"
printf '  Extras    cad: %s\n' "$([ "$CAD" -eq 1 ] && printf yes || printf no)"

# 真·可交互判定：stty 能对 /dev/tty 做终端操作才交互。curl|sh 的 stdin 是脚本本身，
# 但 setup 从 /dev/tty 读，所以这里探 /dev/tty 而非 stdin。CI / docker RUN / 非 tty SSH → 打印命令收工。
if [ -z "${NO_SETUP:-}" ] && stty -a </dev/tty >/dev/null 2>&1; then
  printf '\n== 进入配置向导（ragkernel setup）==\n'
  "$UV" run --directory "$DIR" ragkernel setup < /dev/tty
else
  # uv 可能刚装到 ~/.local/bin、还没进当前 shell 的 PATH——PATH 里没有就打印发现到的绝对路径，
  # 保证这条 Next 命令复制即可用（否则 `uv run` 会 command not found）。
  if command -v uv >/dev/null 2>&1; then RUN=uv; else RUN="$UV"; fi
  printf '\nNext: cd %s && %s run ragkernel setup\n' "$DIR" "$RUN"
fi
