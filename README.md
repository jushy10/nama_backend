# nama_backend

A basic Go HTTP backend.

## Requirements

- Go 1.26+

## Run

```sh
go run .
```

The server listens on `:8080` by default. Override the port with the `PORT`
environment variable:

```sh
PORT=3000 go run .
```

## Endpoints

| Method | Path       | Description                       |
| ------ | ---------- | --------------------------------- |
| GET    | `/`        | Service greeting (JSON)           |
| GET    | `/healthz` | Liveness check (JSON)             |

Example:

```sh
curl localhost:8080/healthz
# {"status":"ok","time":"2026-06-20T00:00:00Z"}
```

## Test

```sh
go test ./...
```

## Build

```sh
go build -o nama_backend .
```

## Contributing

`main` is protected — push to a feature branch and open a pull request. Direct
pushes to `main` are rejected.
