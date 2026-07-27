# F-Droid repository generator.
#
# Debian rather than Alpine (unlike geoblock_watcher) because fdroidserver and
# its APK-parsing stack (androguard) are packaged there, and building them from
# PyPI drags in a compiler toolchain.
#
# --no-install-recommends is not cosmetic: it takes this from ~269 packages /
# ~300 MB to ~67 / ~78 MB. The difference is matplotlib + tk, which arrive as
# Recommends of androguard and which `fdroid update` never touches.
#
# default-jdk-headless is NOT redundant with any other JDK: fdroidserver signs
# the index jar with /usr/lib/jvm/default-java/bin/jarsigner and
# unconditionally prefers that path over every other JDK on the machine
# (common.py: "always prefer the built-in"), without checking that the binary
# is actually there. It ignores JAVA_HOME, and config.yml cannot override it —
# so if the default is a JRE, a run dies at the very last step with an opaque
# OSError. Sanity check after building:
#     docker compose run --rm --entrypoint sh fdroid \
#       -c 'test -x /usr/lib/jvm/default-java/bin/jarsigner && echo ok'
#
# python3-requests and python3-yaml (used by sync.py) come in as dependencies
# of fdroidserver and androguard respectively, so they need no separate mention.
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        fdroidserver \
        default-jdk-headless \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY sync.py .

CMD ["python3", "-u", "sync.py"]
