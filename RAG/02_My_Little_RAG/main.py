import os
from fastapi import FastAPI
from openapi_routes import router as openapi_router

# Get the transport type from environment variable
TRANSPORT_TYPE = os.getenv("TRANSPORT_TYPE", "both").lower()

# Conditionally create and run the appropriate server based on TRANSPORT_TYPE
if TRANSPORT_TYPE == "mcp":
    # Only MCP mode: use the pre-configured MCP server app from retrieval_api.
    # retrieval_api.py registers all five tools (search, search_by_file,
    # list_collections, text_search, text_search_by_file) on a FastMCP instance
    # and exposes ``app = mcp.streamable_http_app()``.
    from retrieval_api import app
elif TRANSPORT_TYPE == "openapi":
    # Only OpenAPI mode: Run FastAPI server with OpenAPI endpoints
    app = FastAPI(
        title="My Little RAG Retrieval Service",
        description="A retrieval service with hybrid search capabilities using dense and sparse vectors",
        version="1.0.0"
    )

    # Include OpenAPI routes
    app.include_router(
        openapi_router,
        prefix="/api",
        tags=["retrieval"]
    )

else:
    # Default to OpenAPI mode when both is specified
    app = FastAPI(
        title="My Little RAG Retrieval Service",
        description="A retrieval service with hybrid search capabilities using dense and sparse vectors",
        version="1.0.0"
    )

    # Include OpenAPI routes
    app.include_router(
        openapi_router,
        prefix="/api",
        tags=["retrieval"]
    )

    @app.get("/")
    async def root():
        return {
            "message": "My Little RAG Retrieval Service - OpenAPI Mode",
            "available_services": ["OpenAPI"],
            "docs": "/docs"
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )
