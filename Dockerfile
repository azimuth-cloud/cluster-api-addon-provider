# Build args which are used in FROM statements must come before
# All FROM statements in the dockerfile.
ARG FINAL_IMAGE_TAG=nonroot

FROM ubuntu:24.04 AS helm

RUN apt-get update && \
    apt-get install -y curl wget ca-certificates

ARG HELM_VERSION=v3.21.2
RUN set -ex; \
    OS_ARCH="$(uname -m)"; \
    case "$OS_ARCH" in \
        x86_64) helm_arch=amd64 ;; \
        aarch64) helm_arch=arm64 ;; \
        *) false ;; \
    esac; \
    wget -q -O - https://get.helm.sh/helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz | \
      tar -xz --strip-components 1 -C /usr/bin linux-${helm_arch}/helm; \
    helm version

############################
## INSTALL AND BUILD APP ###
############################
FROM astral/uv:trixie AS build
# Note: distro should match final image stage to ensure build compat. trixie=debian13
# Non-slim version required for git.

# These env vars setup UV for a docker installation as opposed to
# the defaults which are optimised for local development.
# https://docs.astral.sh/uv/guides/integration/docker/#optimizations
# https://docs.astral.sh/uv/reference/environment/
ENV UV_NO_DEV=1 \
    UV_NO_EDITABLE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_CACHE_DIR=/uv-cache/ \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_INSTALL_BIN=0 \
    UV_PYTHON_PREFERENCE=only-managed

WORKDIR /app-source

### INSTALL PINNED PYTHON ###
# https://docs.astral.sh/uv/guides/install-python/
COPY .python-version /app-source
RUN uv python install \
    && chmod -R ugo=rX /python

### INSTALL PROJECT INTO VENV ###
# uv sync --active makes uv (re)create the currently
# active venv and install into it.
ENV VIRTUAL_ENV=/app

### INSTALL PINNED DEPENDENCIES ###
# --frozen makes uv install the versions which are pinned in uv.lock
# Installing the dependencies first optimizes caching so if the app
# changes but not the deps there is no need to rebuild this.
COPY uv.lock pyproject.toml README.md /app-source
RUN --mount=type=cache,target=/uv-cache/ \
    uv sync --active \
            --frozen \
            --no-install-project \
    && chmod -R ugo=rX /app

### INSTALL THE PROJECT ###
# Then install the app.
COPY ./capi_addons ./capi_addons
RUN --mount=type=cache,target=/uv-cache/ \
    uv sync --active \
            --frozen \
    && chmod -R ugo=rX /app
RUN uv pip install .

RUN ls /app/lib/python3.12/site-packages

###########################
### COMPILE FINAL IMAGE ###
###########################
FROM ubuntu:24.04 AS final

# Create the user that will be used to run the app
ENV APP_UID=1001
ENV APP_GID=1001
ENV APP_USER=app
ENV APP_GROUP=app
RUN groupadd --gid $APP_GID $APP_GROUP && \
    useradd \
      --no-create-home \
      --no-user-group \
      --gid $APP_GID \
      --shell /sbin/nologin \
      --uid $APP_UID \
      $APP_USER

# Tell Helm to use /tmp for mutable data
ENV HELM_CACHE_HOME=/tmp/helm/cache
ENV HELM_CONFIG_HOME=/tmp/helm/config
ENV HELM_DATA_HOME=/tmp/helm/data

### COPY IN APP ###
# Files copied in must have their modes set so the nonroot user may read them.
# You cannot do it here, chmod does not exist, and the docker flag doesn't work with podman yet.
# Should be ordered the same as the stages are created above, to allow the parallel build to keep up.
COPY --from=build /python /python
COPY --from=build /app /app

COPY --from=helm /usr/bin/helm /usr/bin/helm

USER $APP_UID
CMD ["/app/bin/kopf", "run", "--module", "capi_addons.operator", "--all-namespaces", "--verbose"]
