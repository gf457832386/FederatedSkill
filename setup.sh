#!/usr/bin/env bash
# One-click setup for FederatedSkill.
#
# Does the following, in order:
#   1. Installs `uv` (Python package manager) if missing.
#   2. Clones SkillFlow upstream into external/SkillFlow (or pulls latest if
#      it's already there).
#   3. Runs `uv sync` inside SkillFlow to create its venv with harbor + libs.
#   4. Adds SkillFlow's source root to that venv via a `.pth` file so
#      `libs.skill_evolution`, `libs.harbor_noinstall_agents`, etc. are
#      importable from anywhere (not just from inside SkillFlow's cwd).
#   5. Installs federate_skill (the federation adapter, this repo) as an
#      editable package into the same venv.
#   6. Symlinks `.venv` at the federate_skill root → `external/SkillFlow/.venv`
#      so users `source .venv/bin/activate` like in any normal project.
#   7. Copies `.env.example` → `.env` if missing.
#   8. Downloads test_tasks/ from HuggingFace.
#   9. Builds the harbor-cli base image from SkillFlow's docker/harbor-cli-base/
#      (NOT pulled from a registry; the base image isn't public). Tagged under
#      both skillflow/* and skillevlove/* so unmodified task Dockerfiles work.
#      This is the slow step (~15-30 min, network heavy) and runs once.
#
# Tunables (export before running):
#   PYTHON                 default: python3
#   SKILLFLOW_REPO         default: https://github.com/ZhangZi-a/SkillFlow.git
#   SKILLFLOW_REF          default: main  (branch/tag/commit to check out)
#   HF_DATASET             default: zhang-ziao/SkillFlow-Task
#   SKIP_TASKS_DOWNLOAD=1  skip the HuggingFace dataset download
#   SKIP_IMAGE_BUILD=1     skip the base image build (fed settings 2-4 will fail without it)
#
# After this script, the venv is ready. Activate with:
#     source .venv/bin/activate

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${PYTHON:-python3}"
SKILLFLOW_REPO="${SKILLFLOW_REPO:-https://github.com/ZhangZi-a/SkillFlow.git}"
SKILLFLOW_REF="${SKILLFLOW_REF:-main}"
HF_DATASET="${HF_DATASET:-zhang-ziao/SkillFlow-Task}"

step() { printf "\n[setup] %s\n" "$*"; }
ok()   { printf "[setup]   ✓ %s\n" "$*"; }
warn() { printf "[setup]   ! %s\n" "$*"; }

# ---------------------------------------------------------------------------
# 1) uv
# ---------------------------------------------------------------------------
step "ensuring uv is installed"
if ! command -v uv >/dev/null 2>&1; then
    if [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    else
        echo "  [setup] installing uv via the official installer …"
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
        export PATH="$HOME/.local/bin:$PATH"
    fi
fi
command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv not on PATH after install attempt. Add \$HOME/.local/bin to PATH and retry." >&2
    exit 1
}
ok "uv $(uv --version | awk '{print $2}')"

# ---------------------------------------------------------------------------
# 2) Clone or update SkillFlow
# ---------------------------------------------------------------------------
step "cloning SkillFlow upstream into external/SkillFlow"
mkdir -p external
if [ -d external/SkillFlow/.git ]; then
    (cd external/SkillFlow && git fetch --quiet origin && git checkout --quiet "$SKILLFLOW_REF" && git pull --quiet --ff-only)
    ok "external/SkillFlow updated to $SKILLFLOW_REF"
else
    git clone --quiet "$SKILLFLOW_REPO" external/SkillFlow
    (cd external/SkillFlow && git checkout --quiet "$SKILLFLOW_REF")
    ok "external/SkillFlow cloned at $SKILLFLOW_REF"
fi

# ---------------------------------------------------------------------------
# 3) uv sync inside SkillFlow (creates external/SkillFlow/.venv with harbor +
#    all SkillFlow deps)
# ---------------------------------------------------------------------------
step "uv sync inside external/SkillFlow (installs harbor + SkillFlow deps)"
(cd external/SkillFlow && uv sync --quiet)
ok "external/SkillFlow/.venv ready"

# ---------------------------------------------------------------------------
# 4) Make SkillFlow source importable from anywhere via .pth
# ---------------------------------------------------------------------------
step "adding SkillFlow source root to the venv's import path"
SITE_PACKAGES="$(external/SkillFlow/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
SKILLFLOW_ROOT="$(realpath external/SkillFlow)"
PTH="$SITE_PACKAGES/skillflow_source.pth"
printf "%s\n" "$SKILLFLOW_ROOT" > "$PTH"
ok "wrote $PTH"

# ---------------------------------------------------------------------------
# 5) Install federate_skill into the same venv (reconciles harbor pin too)
# ---------------------------------------------------------------------------
step "installing federate_skill into external/SkillFlow/.venv"
VIRTUAL_ENV="$HERE/external/SkillFlow/.venv" uv pip install --quiet -e .
ok "federate_skill installed (editable)"

# ---------------------------------------------------------------------------
# 6) .venv symlink at the repo root for convenience
# ---------------------------------------------------------------------------
step "symlinking .venv → external/SkillFlow/.venv"
if [ -L .venv ] || [ -d .venv ]; then
    rm -rf .venv
fi
ln -s external/SkillFlow/.venv .venv
ok ".venv -> external/SkillFlow/.venv"

# ---------------------------------------------------------------------------
# 7) API-key scaffold
# ---------------------------------------------------------------------------
step "API-key file"
if [ -f .env ]; then
    ok ".env already present (will NOT overwrite)"
else
    cp .env.example .env
    warn ".env created from .env.example — fill in DASHSCOPE_KEY / MOONSHOT_KEY before running"
fi

# ---------------------------------------------------------------------------
# 8) Download test_tasks/ from HuggingFace
# ---------------------------------------------------------------------------
#    The HF dataset packs everything under a top-level `test_tasks/` directory
#    plus a README.md. Downloading with --local-dir=test_tasks would produce
#    test_tasks/test_tasks/<family> (double-nested), so we download into a
#    staging dir and then move the inner test_tasks/ up.
#
#    HuggingFace's HEAD-stat phase often rate-limits us (HTTP 429) on the
#    smaller files at the end of the run; the actual byte download usually
#    completes before that. We tolerate a non-zero `hf` exit when the
#    staging dir already has all 20 family directories.
step "downloading test_tasks/ from HuggingFace ($HF_DATASET, ~1.6 GB)"
if [ -n "${SKIP_TASKS_DOWNLOAD:-}" ]; then
    warn "SKIP_TASKS_DOWNLOAD=1 set, not downloading (federation runs will fail without test_tasks/)"
elif [ -d test_tasks ] && [ "$(ls test_tasks 2>/dev/null | grep -v '^HF_DATASET' | wc -l)" -ge 20 ]; then
    ok "test_tasks/ already present with $(ls test_tasks | grep -v '^HF_DATASET' | wc -l) families"
else
    STAGE_DIR="$(mktemp -d -t skillflow_task_dl.XXXXXX)"
    set +e
    .venv/bin/hf download "$HF_DATASET" --repo-type dataset --local-dir "$STAGE_DIR"
    DL_RC=$?

    set -e
    if [ -d "$STAGE_DIR/test_tasks" ]; then
        N_FAM="$(ls "$STAGE_DIR/test_tasks" 2>/dev/null | wc -l)"
        if [ "$N_FAM" -lt 20 ]; then
            warn "HF download finished with $N_FAM/20 families (rc=$DL_RC); re-running setup.sh will resume"
        fi
        mkdir -p test_tasks
        # Move (or merge) family dirs over
        for fam in "$STAGE_DIR/test_tasks"/*; do
            [ -d "$fam" ] || continue
            mv "$fam" test_tasks/
        done
        [ -f "$STAGE_DIR/README.md" ] && mv "$STAGE_DIR/README.md" test_tasks/HF_DATASET_README.md
    else
        warn "HF download produced no test_tasks/ in staging (rc=$DL_RC) — re-run setup.sh to retry"
    fi
    rm -rf "$STAGE_DIR"
    ok "test_tasks/ ready ($(ls test_tasks 2>/dev/null | grep -v '^HF_DATASET' | wc -l) families)"
fi

# ---------------------------------------------------------------------------
# 9) Container base image
# ---------------------------------------------------------------------------
#    Every task ships its own Dockerfile in test_tasks/<family>/<task>/
#    environment/. Those Dockerfiles `FROM` a shared base image that bakes in
#    the four agent CLIs (claude-code / qwen-code / kimi-cli / codex / gemini).
#    The base image is NOT on a public registry, so we build it locally from
#    SkillFlow's docker/harbor-cli-base/ once (~15-30 min, network heavy).
#    Subsequent per-task images build on top of the cached base in seconds.
#
#    Tag the result under both names: skillflow/* (SkillFlow's official name)
#    and skillevlove/* (the legacy name the HF dataset's Dockerfiles still
#    reference). This lets unmodified task Dockerfiles resolve their FROM
#    line without our needing to rewrite them.
step "container base image"
BASE_SKILLFLOW="skillflow/harbor-cli-base:ubuntu24.04"
BASE_LEGACY="skillevlove/harbor-cli-openhands:ubuntu24.04"

if command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
elif command -v docker >/dev/null 2>&1; then
    RUNTIME=docker
else
    RUNTIME=""
fi

if [ -z "$RUNTIME" ]; then
    warn "neither podman nor docker on PATH — fed settings 2-4 won't run"
elif [ -n "${SKIP_IMAGE_BUILD:-}" ]; then
    warn "SKIP_IMAGE_BUILD=1 set, not building (fed settings 2-4 require the base image)"
else
    # Check if either tag already exists
    if $RUNTIME image inspect "$BASE_SKILLFLOW" >/dev/null 2>&1 || $RUNTIME image inspect "$BASE_LEGACY" >/dev/null 2>&1; then
        ok "base image already present"
        # Ensure both tags exist for downstream Dockerfile FROM lines
        if $RUNTIME image inspect "$BASE_SKILLFLOW" >/dev/null 2>&1 && ! $RUNTIME image inspect "$BASE_LEGACY" >/dev/null 2>&1; then
            $RUNTIME tag "$BASE_SKILLFLOW" "$BASE_LEGACY"
            ok "tagged $BASE_SKILLFLOW → $BASE_LEGACY"
        elif $RUNTIME image inspect "$BASE_LEGACY" >/dev/null 2>&1 && ! $RUNTIME image inspect "$BASE_SKILLFLOW" >/dev/null 2>&1; then
            $RUNTIME tag "$BASE_LEGACY" "$BASE_SKILLFLOW"
            ok "tagged $BASE_LEGACY → $BASE_SKILLFLOW"
        fi
    else
        warn "building base image from external/SkillFlow/docker/harbor-cli-base/ (this takes 15-30 min and ~1.5 GB of network traffic)"
        # SkillFlow's build.sh hard-codes `docker build`; substitute our runtime.
        if [ "$RUNTIME" = "podman" ]; then
            (cd external/SkillFlow/docker/harbor-cli-base && podman build -t "$BASE_SKILLFLOW" .)
        else
            (cd external/SkillFlow/docker/harbor-cli-base && bash build.sh "$BASE_SKILLFLOW")
        fi
        $RUNTIME tag "$BASE_SKILLFLOW" "$BASE_LEGACY"
        ok "base image built and tagged under both names"
    fi
fi

# ---------------------------------------------------------------------------
# Sanity check
# ---------------------------------------------------------------------------
step "sanity-check imports"
.venv/bin/python - <<'PY'
from skillfl.harbor_runner import UserSpec
from skillfl.skillflow_adapter.cli import load_job_config
from skillfl.skillflow_adapter.merge import CloudSkillMerge, RewardWeightedFileMerge
from libs.skill_evolution.patcher import TrajectoryCompactor
from libs.harbor_noinstall_agents.agents import NoInstallClaudeCode
from harbor.models.job.config import JobConfig
print("[setup]   ✓ all imports OK")
PY

cat <<EOF

[setup] done.

Next steps:
  1. Edit .env and fill in the API keys you'll use
       (DASHSCOPE_KEY for qwen/glm, MOONSHOT_KEY for kimi)
  2. Activate the venv:
       source .venv/bin/activate
  3. Run one of the four settings:
       scripts/run_1_se.sh                    # Self-Evolve baseline
       scripts/run_2_fed_3glm_cc.sh           # 3 GLM workers, claude-code
       scripts/run_3_fed_hetero_cc.sh         # qwen/glm/kimi on claude-code
       scripts/run_4_fed_hetero_mixed_cli.sh  # qwen-code + claude-code + kimi-cli
  Each run script takes an optional FAMILY env var:
       FAMILY=Production-Capacity-Planning scripts/run_3_fed_hetero_cc.sh
  By default it sweeps all 20 families in test_tasks/.
EOF
