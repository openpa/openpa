# Docker Setup for OPENPA Agent

This document explains how to build and run the OPENPA Agent using Docker.

## Prerequisites

- Docker installed on your system
- Docker Compose (optional, for multi-container setup)
- `.env` file with required API keys

## Quick Start

### Using Docker Compose (Recommended)

1. Create a `.env` file with your API keys:
   ```bash
   OPENAI_API_KEY=your_openai_api_key_here
   GROQ_API_KEY=your_groq_api_key_here
   ```

2. Start all services:
   ```bash
   docker-compose up -d
   ```

3. Check logs:
   ```bash
   docker-compose logs -f olli-agent
   ```

4. Stop services:
   ```bash
   docker-compose down
   ```

### Using Docker Only

1. Build the image:
   ```bash
   docker build -t olli-agent:latest .
   ```

2. Run the container:
   ```bash
   docker run -d \
     --name olli-agent \
     -p 10000:10000 \
     -e OPENAI_API_KEY=your_key_here \
     -e GROQ_API_KEY=your_key_here \
     olli-agent:latest
   ```

   Or use an env file:
   ```bash
   docker run -d \
     --name olli-agent \
     -p 10000:10000 \
     --env-file .env \
     olli-agent:latest
   ```

## Environment Variables

Key environment variables you can configure:

- `OPENAI_API_KEY` - OpenAI API key (required)
- `GROQ_API_KEY` - Groq API key (optional)
- `HOST` - Server host (default: 0.0.0.0)
- `PORT` - Server port (default: 10000)
- `DEBUG` - Enable debug mode (default: false)
- `LOG_LEVEL` - Logging level (default: INFO)

See [app/config/settings.py](app/config/settings.py) for all available environment variables.

## Accessing the Application

Once running, the application will be available at:
- http://localhost:10000

## Troubleshooting

### Container won't start
```bash
# Check logs
docker logs olli-agent

# Or with docker-compose
docker-compose logs olli-agent
```

### Port already in use
Change the port mapping in `docker-compose.yml` or in your `docker run` command:
```bash
# Map to a different port (e.g., 8080)
docker run -p 8080:10000 ...
```

## Development

To rebuild after code changes:
```bash
# With docker-compose
docker-compose up -d --build

# With docker
docker build -t olli-agent:latest .
docker stop olli-agent && docker rm olli-agent
docker run -d --name olli-agent -p 10000:10000 --env-file .env olli-agent:latest
```

## Production Considerations

1. **Use secrets management** - Don't hardcode API keys in docker-compose.yml
2. **Add volume mounts** - For persistent data if needed
3. **Configure logging** - Set up proper log aggregation
4. **Use specific tags** - Don't use `latest` in production
5. **Resource limits** - Add memory and CPU limits in docker-compose.yml

Example with resource limits:
```yaml
services:
  olli-agent:
    # ... other config
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
```
