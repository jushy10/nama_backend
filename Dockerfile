# syntax=docker/dockerfile:1

# ---- Build stage: compile the Go binary --------------------------------------
# We use the full Go image here because it has the compiler and toolchain.
FROM golang:1.26-alpine AS build

WORKDIR /src

# Copy module files first and download deps. Docker caches this layer, so deps
# are only re-downloaded when go.mod/go.sum change — not on every code edit.
COPY go.mod go.sum* ./
RUN go mod download

# Now copy the rest of the source and build a fully static binary.
# CGO_ENABLED=0 means "no C dependencies" -> the binary runs on a tiny base image.
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /bin/nama_backend .

# ---- Run stage: the image that actually ships --------------------------------
# distroless/static has NO shell, package manager, or OS clutter — just enough
# to run a static binary. Smaller image = faster deploys + smaller attack surface.
# "nonroot" runs the process as an unprivileged user (a security best practice).
FROM gcr.io/distroless/static-nonroot

COPY --from=build /bin/nama_backend /nama_backend

# Documents the port the app listens on (matches PORT default in main.go).
EXPOSE 8080
USER nonroot:nonroot

ENTRYPOINT ["/nama_backend"]
