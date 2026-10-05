# The WhatsApp bridge: a small, long-lived Node process holding the Baileys connection.
# auth/ (the linked session's keys) lives on a named volume -- see docker-compose.yml -- so
# a container restart or rebuild never forces re-pairing.
FROM node:22-slim

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY tsconfig.json ./
COPY src/ ./src/

VOLUME ["/app/auth"]
EXPOSE 3000
CMD ["npm", "start"]
