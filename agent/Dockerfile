# Build from the repository root:
#   docker build -f agent/Dockerfile -t coach-agent .
FROM python:3.12-slim
WORKDIR /app
COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -q -r agent/requirements.txt
COPY strength_coach strength_coach
COPY agent agent
COPY persona-system-prompt.md .
ENV COACH_DATA_DIR=/data
WORKDIR /app/agent
EXPOSE 8000
# "--host 0" binds all IPv4 interfaces ("0" resolves to the unspecified address).
# Publish the port only to the interfaces you intend (for example bind the
# published port to loopback in `docker run -p`).
CMD ["uvicorn", "service:app", "--host", "0", "--port", "8000"]
