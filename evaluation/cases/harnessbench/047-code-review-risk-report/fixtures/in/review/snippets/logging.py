def log_request(request):
    logger.info("request completed", extra={"path": request.path})
