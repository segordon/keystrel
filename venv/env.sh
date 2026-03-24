#!/usr/bin/env bash
# shellcheck shell=bash

_stt_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${STT_VENV_DIR:-}" ]]; then
  export VIRTUAL_ENV="$STT_VENV_DIR"
elif [[ -x "$_stt_env_dir/bin/python" ]]; then
  export VIRTUAL_ENV="$_stt_env_dir"
else
  export VIRTUAL_ENV="$HOME/.venvs/faster-whisper"
fi

if [[ ! -x "$VIRTUAL_ENV/bin/python" ]]; then
  printf '[stt-env] expected python at %s\n' "$VIRTUAL_ENV/bin/python" >&2
  printf '[stt-env] set STT_VENV_DIR to your faster-whisper venv path\n' >&2
  return 2 2>/dev/null || exit 2
fi

export PATH="$VIRTUAL_ENV/bin:$PATH"

_stt_site_packages=""
for candidate in "$VIRTUAL_ENV"/lib/python*/site-packages; do
  if [[ -d "$candidate" ]]; then
    _stt_site_packages="$candidate"
    break
  fi
done

_stt_cuda_libs=""
if [[ -n "$_stt_site_packages" ]]; then
  if [[ -d "$_stt_site_packages/nvidia/cublas/lib" ]]; then
    _stt_cuda_libs="$_stt_site_packages/nvidia/cublas/lib"
  fi
  if [[ -d "$_stt_site_packages/nvidia/cudnn/lib" ]]; then
    _stt_cuda_libs="${_stt_cuda_libs:+$_stt_cuda_libs:}$_stt_site_packages/nvidia/cudnn/lib"
  fi
fi

if [[ -n "$_stt_cuda_libs" ]]; then
  export LD_LIBRARY_PATH="$_stt_cuda_libs:${LD_LIBRARY_PATH:-}"
fi

unset _stt_env_dir
unset _stt_site_packages
unset _stt_cuda_libs
