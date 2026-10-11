# ---- 构建阶段 ----
FROM node:20-alpine AS build

WORKDIR /app

# 先装依赖，利用层缓存（package.json 不变就不重装）
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---- 运行阶段 ----
FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
