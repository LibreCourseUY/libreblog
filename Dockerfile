FROM hugomods/hugo:exts AS builder
WORKDIR /src
COPY . .
RUN hugo --minify

FROM nginx:alpine
COPY --from=builder /src/public /usr/share/nginx/html