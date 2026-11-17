# 前端镜像：nginx + dist 构建产物。
# dist 由宿主机 npm run build 生成后 COPY 进来——与 api 镜像同一约定：
# 镜像构建机不依赖 node（国内拉 node 基础镜像也不稳定）。
# 改前端代码后重新交付 = 宿主机 npm run build && docker compose build web。
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY dist /usr/share/nginx/html

EXPOSE 80
