#!/bin/sh
set -eu

repository_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tools_directory="$repository_root/.tools/bin"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

shellcheck_version=0.11.0
actionlint_version=1.7.12
hadolint_version=2.14.0

case "$(uname -m)" in
  x86_64|amd64)
    shellcheck_arch=x86_64
    shellcheck_sha=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
    actionlint_arch=amd64
    actionlint_sha=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
    hadolint_arch=x86_64
    hadolint_sha=6bf226944684f56c84dd014e8b979d27425c0148f61b3bd99bcc6f39e9dc5a47
    ;;
  aarch64|arm64)
    shellcheck_arch=aarch64
    shellcheck_sha=12b331c1d2db6b9eb13cfca64306b1b157a86eb69db83023e261eaa7e7c14588
    actionlint_arch=arm64
    actionlint_sha=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
    hadolint_arch=arm64
    hadolint_sha=331f1d3511b84a4f1e3d18d52fec284723e4019552f4f47b19322a53ce9a40ed
    ;;
  *)
    echo "Unsupported analysis-tool architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$tools_directory"

download() {
  url=$1
  destination=$2
  expected_sha=$3
  curl --fail --location --silent --show-error "$url" --output "$destination"
  printf '%s  %s\n' "$expected_sha" "$destination" | sha256sum --check --status
}

shellcheck_archive="$temporary_directory/shellcheck.tar.xz"
download \
  "https://github.com/koalaman/shellcheck/releases/download/v${shellcheck_version}/shellcheck-v${shellcheck_version}.linux.${shellcheck_arch}.tar.xz" \
  "$shellcheck_archive" \
  "$shellcheck_sha"
tar -xJf "$shellcheck_archive" -C "$temporary_directory"
cp "$temporary_directory/shellcheck-v${shellcheck_version}/shellcheck" \
  "$tools_directory/shellcheck"

actionlint_archive="$temporary_directory/actionlint.tar.gz"
download \
  "https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/actionlint_${actionlint_version}_linux_${actionlint_arch}.tar.gz" \
  "$actionlint_archive" \
  "$actionlint_sha"
tar -xzf "$actionlint_archive" -C "$temporary_directory" actionlint
cp "$temporary_directory/actionlint" "$tools_directory/actionlint"

hadolint_download="$temporary_directory/hadolint"
download \
  "https://github.com/hadolint/hadolint/releases/download/v${hadolint_version}/hadolint-linux-${hadolint_arch}" \
  "$hadolint_download" \
  "$hadolint_sha"
cp "$hadolint_download" "$tools_directory/hadolint"

chmod 0755 \
  "$tools_directory/shellcheck" \
  "$tools_directory/actionlint" \
  "$tools_directory/hadolint"

echo "Installed pinned analysis tools in $tools_directory"
