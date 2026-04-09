FROM python:3.13-slim as builder

WORKDIR /app

# Upgrade pip and install uv
RUN pip install --upgrade pip
RUN pip install uv

# Copy application code
COPY . .

# Install the current project after copying all files
RUN uv sync

# Expose the port
EXPOSE 10000

# Run the application
CMD uv run .
# CMD sleep infinity
