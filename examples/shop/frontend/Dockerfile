FROM node:22.23.1-alpine AS build
WORKDIR /app
COPY examples/shop/frontend/package*.json ./
RUN npm ci
COPY examples/shop/frontend/ ./
RUN npm run build
FROM nginx:1.28-alpine
COPY examples/shop/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
