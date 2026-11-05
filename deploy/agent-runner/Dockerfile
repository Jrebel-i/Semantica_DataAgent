FROM node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da

RUN npm install --global --omit=dev @earendil-works/pi-coding-agent@1.2.0 \
    @anthropic-ai/claude-code@2.1.318 \
    @openai/codex@0.166.0 \
    && npm cache clean --force

WORKDIR /workspace
ENTRYPOINT ["pi"]
