import asyncio
from typing import Any, NoReturn

import websockets
from config import BLACK_LIST, SelfService, logger
from fastapi import (APIRouter, HTTPException, Request, WebSocket,
                     WebSocketDisconnect, WebSocketException, status)
from fastapi.responses import JSONResponse
from patisson_request.errors import (DuplicatHeadersError, ErrorCode,
                                     ErrorSchema)
from patisson_request.service_requests import HttpxPostData
from patisson_request.service_routes import url_params
from patisson_request.services import Service
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import InvalidStatus

router = APIRouter()

SELF_SERVICE_PARAMETERS: dict[str, list[str]] = {
    "GET": [
        "_use_cache", "_cache_lifetime", 
        "_max_reconnections", "_timeout"
        ],
    "POST": [
        "_max_reconnections", "_timeout", 
        "_is_graphql", "_use_graphql_cache",
        "_graphql_cache_lifetime"
        ]
    }
FALSE_STR = 'False'

@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(request: Request, path: str):
    
    logger.info(f"Received {request.method} request for path: {path}")
    try:
        route_segments = path.split("/")
        service_segment = route_segments[0]
        path_segment = "/".join(route_segments[1:])
        service = Service(service_segment)
        
        if service in BLACK_LIST or service not in SelfService.external_services:
            logger.warning(f"Service '{service_segment}' is blacklisted or invalid.")
            raise
        
    except Exception as e:
        logger.warning(f"Service validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=[ErrorSchema(
                error=ErrorCode.INVALID_PARAMETERS,
                extra='The request for the specified service was rejected'
            ).model_dump()]
        )
    
    params: dict[str, str] = {}
    kwargs: dict[str, Any] = {}
    for param in request.query_params:
        if param in SELF_SERVICE_PARAMETERS[request.method]:
            key = param[1:]
            if param == FALSE_STR:
                kwargs[key] = False
            kwargs[key] = request.query_params[param]
        else:
            params[param] = request.query_params[param]
    path = path_segment + '?' + url_params(**params)
    kwargs['_return_nonetype_response_body'] = True
    
    headers = dict(request.headers)
    headers.pop("content-length", None)
    
    try:
        match request.method:
            case "GET":
                response = await SelfService.get_request(
                    service=service, path=path, 
                    response_type=dict,  # type: ignore[reportArgumentType]
                    add_headers=headers, **kwargs
                )
            case "POST":
                data = {
                    'json': request.json(),
                    'data': request.form(),
                    'content': request.body()
                }
                for key in data:
                    try:
                        data[key] = await data[key]
                    except Exception as e:
                        data[key] = None
                
                if 'is_graphql' not in kwargs and 'graphql' in path:
                    kwargs['is_graphql'] = True
                
                response = await SelfService.post_request(
                    service=service, path=path,
                    response_type=dict,  # type: ignore[reportArgumentType]
                    post_data=HttpxPostData(**data), **kwargs
                )
            case _:
                logger.warning(f"Method {request.method} not allowed.")
                raise HTTPException(
                    status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                    detail=ErrorSchema(
                        error=ErrorCode.INVALID_PARAMETERS,
                        extra=f'method {request.method} not allowed'
                    ).model_dump()
                )
                
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[ErrorSchema(
                error=ErrorCode.INVALID_PARAMETERS,
                extra=str(e)
            ).model_dump()]
        )
        
    except DuplicatHeadersError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=[ErrorSchema(
                error=ErrorCode.INVALID_PARAMETERS,
                extra=str(e)
            ).model_dump()]
        )
    
    if not isinstance(response.body, dict):
        response.body = response.body.model_dump()  # 4xx and 5xx codes
    return JSONResponse(
        content=response.body,
        status_code=response.status_code
    )


@router.websocket("/{path:path}")
async def websocket_proxy(path: str, websocket: WebSocket):
    
    try:
        service = Service(path.split("/")[0])
        if service in BLACK_LIST or service not in SelfService.external_services:
            e = f"Service '{service}' is blacklisted or invalid."
            logger.warning(e)
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=e)
        
        await websocket.accept()

        x_client_token = websocket.headers.get("X-Client-Token")
        if not x_client_token:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, 
                                     reason="Missing X-Client-Token header")
        
        upstream_url = f"ws://{SelfService.default_host}/{path}" 
        async with websockets.connect(
            upstream_url, additional_headers=[
                ('X-Client-Token', x_client_token),
                ('Authorization', SelfService.access_token)
            ]) as upstream_ws:
            
            async def forward_messages(src_ws: WebSocket, dst_ws: ClientConnection) -> NoReturn:
                while True:
                    data = await src_ws.receive_text()
                    await dst_ws.send(data)
                    
            async def backward_messages(src_ws: ClientConnection, dst_ws: WebSocket) -> NoReturn:
                while True:
                    data = await src_ws.recv()
                    await dst_ws.send_text(str(data))
                        
            await asyncio.gather(
                forward_messages(websocket, upstream_ws),
                backward_messages(upstream_ws, websocket)
            )

    except (WebSocketDisconnect, websockets.ConnectionClosedError) as e:
        try:
            await websocket.close(code=e.code, reason=e.reason)
        except: pass
        
    except InvalidStatus as e:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, 
                                 reason=f'{str(e.response.status_code)} HTTP code')
        
    except HTTPException as e:
        raise WebSocketException(code=e.status_code, reason=e.detail)
    
    except Exception as e:
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except: pass
        raise e