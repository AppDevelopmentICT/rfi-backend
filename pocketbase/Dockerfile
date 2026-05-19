FROM alpine:latest

RUN apk add --no-cache \
    unzip \
    ca-certificates \
    curl \
    jq

# Download and unzip the latest PocketBase release
RUN LATEST_URL=$(curl -s https://api.github.com/repos/pocketbase/pocketbase/releases/latest | jq -r '.assets[] | select(.name | endswith("linux_amd64.zip")).browser_download_url') \
    && curl -L -o /tmp/pb.zip $LATEST_URL \
    && unzip /tmp/pb.zip -d /pb/

# Copy local pb_migrations dir into the image
COPY ./pb_migrations /pb/pb_migrations

EXPOSE 8090

# Start PocketBase
CMD ["/pb/pocketbase", "serve", "--http=0.0.0.0:8090"]
