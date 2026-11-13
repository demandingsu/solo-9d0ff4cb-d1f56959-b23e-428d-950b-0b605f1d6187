FROM python:3.11-slim

WORKDIR /srv

# 构建时生成静态站点（原公告列表 + 静态失败场景目录）
COPY generate_site.py .
RUN python generate_site.py

# 运行时由 serve.py 同时提供静态文件与 /backfill/ 动态演示场景
COPY serve.py .

EXPOSE 80
CMD ["python", "serve.py"]
