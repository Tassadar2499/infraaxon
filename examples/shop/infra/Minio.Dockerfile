FROM golang:1.24.7-bookworm AS build
RUN git clone --depth=1 --branch RELEASE.2025-09-07T16-13-09Z https://github.com/minio/minio.git /src
WORKDIR /src
RUN mkdir /empty-data
RUN CGO_ENABLED=0 go build -trimpath -o /minio .
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /minio /minio
COPY --from=build --chown=65532:65532 /empty-data /data
USER 65532:65532
EXPOSE 9000 9001
ENTRYPOINT ["/minio"]
