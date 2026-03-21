# Build nginx with the ngx_http_geoip2_module baked in.
# The official nginx image ships without GeoIP2 support; we compile the
# dynamic module against the same nginx version used in the base image.

ARG NGINX_VERSION=1.27.4

FROM nginx:${NGINX_VERSION}-alpine AS builder

ARG NGINX_VERSION

RUN apk add --no-cache \
        build-base \
        git \
        libmaxminddb-dev \
        pcre2-dev \
        openssl-dev \
        zlib-dev \
        linux-headers

# Clone the GeoIP2 nginx module at the tag matching the installed nginx
RUN git clone --depth 1 \
        https://github.com/leev/ngx_http_geoip2_module.git \
        /usr/src/ngx_http_geoip2_module

# Fetch the nginx source matching the base image version
RUN wget -q "http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz" \
        -O /usr/src/nginx.tar.gz \
    && tar -xzf /usr/src/nginx.tar.gz -C /usr/src

WORKDIR /usr/src/nginx-${NGINX_VERSION}

# Build only the dynamic module (configure flags from `nginx -V`)
RUN eval $(nginx -V 2>&1 | grep 'configure arguments:' | sed 's/configure arguments://') && \
    ./configure \
        $configure_args \
        --add-dynamic-module=/usr/src/ngx_http_geoip2_module \
    && make modules

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM nginx:${NGINX_VERSION}-alpine

# Runtime dependency for MaxMind DB
RUN apk add --no-cache libmaxminddb

# Copy the compiled dynamic module
COPY --from=builder \
    /usr/src/nginx-${NGINX_VERSION}/objs/ngx_http_geoip2_module.so \
    /usr/lib/nginx/modules/ngx_http_geoip2_module.so

# Main nginx config (loads the dynamic module at the top level)
COPY nginx.conf /etc/nginx/nginx.conf

# GeoIP map directory (populated by geoblock_watcher at runtime)
RUN mkdir -p /etc/nginx/geoblock

EXPOSE 80 443
