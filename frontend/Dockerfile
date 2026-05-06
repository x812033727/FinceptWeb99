# ── Stage 1: dev (used by docker-compose.override.yml) ───────────
FROM node:20-alpine AS dev
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ── Stage 2: build ────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build

# ── Stage 3: production (nginx serves static files) ───────────────
FROM nginx:alpine AS production
COPY --from=build /app/dist /usr/share/nginx/html
# SPA fallback: unknown routes return index.html.
# /assets/ files MUST 404 if missing — falling back to index.html for a
# requested-but-missing hashed bundle (e.g. a service worker holding a
# stale `index.html` referencing a previous build's chunk hash) makes
# the browser try to execute HTML as JS and the page silently fails to
# render. With explicit /assets/ → =404, the browser sees a real load
# error and the SW fetch handler updates instead of caching HTML as JS.
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  index index.html;\n  location /assets/ {\n    try_files $uri =404;\n  }\n  location / {\n    try_files $uri $uri/ /index.html;\n  }\n}\n' \
    > /etc/nginx/conf.d/default.conf
EXPOSE 80
