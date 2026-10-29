# BASE_IMAGE lets CI build against Ubuntu 22.04/24.04 AND Fedora from the same
# Dockerfile (see .github/workflows/test.yml). Defaults to 24.04 for local use.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive

# Bootstrap the base image's OWN package manager — apt (Debian/Ubuntu) or dnf
# (Fedora/RHEL) — so one Dockerfile builds both.
#
# apt: the stock ubuntu images (22.04 and 24.04) include the 'universe'
#   component, so 'autojump' from the 'packages' role is available without
#   extra config.
# dnf: a bare Fedora image also needs tar/findutils (Debian marks them
#   essential; the `tools`/`github_binary` roles use them to unpack release
#   assets) plus shadow-utils/util-linux for `useradd`/`runuser`/`chsh`.
#   `curl` is installed only when missing: Fedora ships `curl-minimal`, which
#   CONFLICTS with the full `curl` package.
#
# Proxy bypass: corporate proxies often 403 on distro archives. apt takes a
# per-host DIRECT directive; dnf resolves through libcurl, which honours
# no_proxy — so we clear the proxy env for that one dnf call instead (uv/git/
# curl keep using the proxy env vars as before).
RUN set -eu; \
    if command -v apt-get >/dev/null 2>&1; then \
      echo 'Acquire::http::Proxy "DIRECT";' > /etc/apt/apt.conf.d/99no-proxy; \
      echo 'Acquire::https::Proxy "DIRECT";' >> /etc/apt/apt.conf.d/99no-proxy; \
      apt-get update; \
      apt-get install -y --no-install-recommends \
            python3 sudo git curl wget ca-certificates openssh-client; \
      rm -rf /var/lib/apt/lists/*; \
    elif command -v dnf >/dev/null 2>&1; then \
      no_proxy='*' NO_PROXY='*' dnf -y install \
            python3 sudo git wget ca-certificates openssh-clients \
            shadow-utils util-linux findutils tar gzip; \
      if ! command -v curl >/dev/null 2>&1; then \
        no_proxy='*' NO_PROXY='*' dnf -y install curl; \
      fi; \
      dnf clean all; \
    else \
      echo "unsupported BASE_IMAGE: need apt-get or dnf" >&2; \
      exit 1; \
    fi

# Non-root ansible user with passwordless sudo. This mirrors a real host where
# become: yes is needed, so the playbook's become paths actually get exercised.
RUN useradd -m -s /bin/bash tester \
  && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester \
  && chmod 0440 /etc/sudoers.d/tester

WORKDIR /opt/standup
COPY . .

# uv is installed as root (to /root/.local); copy the self-contained binary
# to a shared path so the runtime user 'tester' can execute it (a symlink into
# /root would be unreadable once USER switches away from root). Then run uv
# sync AS tester — so the venv and any uv-managed Python land under tester's
# home (readable at runtime) instead of /root (mode 700, unreadable by tester).
# This avoids the "bad interpreter: Permission denied" trap on 22.04, where
# the system python (3.10) is too old (project needs >=3.12) so uv downloads a
# managed CPython — if that download lands under /root, tester can't run it.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
  && cp /root/.local/bin/uv /usr/local/bin/uv \
  && chown -R tester:tester /opt/standup \
  && runuser -u tester -- sh -c 'export HOME=/home/tester && cd /opt/standup && uv sync'

USER tester
ENV PATH="/opt/standup/.venv/bin:$PATH"

# Harness, not a baked result: drop into a shell and run the playbook at
# container time so every test starts from a clean machine.
CMD ["bash"]
