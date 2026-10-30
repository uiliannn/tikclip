"""
统一的后端应用工厂函数
支持 web 和 desktop 两种模式
"""
import logging
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.api.v1 import api_router
from backend.api.v1.health import router as health_router
from backend.core.database import engine
from backend.models.base import Base
from backend.core.config import get_logging_config, get_api_key
from backend.core.error_middleware import global_exception_handler
from backend.core.local_origin_guard import LocalOriginGuard, allowed_origins

logger = logging.getLogger(__name__)

def create_app(mode: str = "web") -> FastAPI:
    """
    创建 FastAPI 应用实例
    
    Args:
        mode: 运行模式，支持 "web" 或 "desktop"
    """
    # 设置模式环境变量
    os.environ["TIKCLIP_MODE"] = mode

    if mode == "desktop":
        from backend.core.windows_asyncio import install_windows_proactor_cleanup
        install_windows_proactor_cleanup()

    try:
        from backend.core.sentry_setup import init_sentry
        init_sentry(mode)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Sentry 初始化失败（忽略）: {e}")
    
    # 配置日志
    logging_config = get_logging_config()
    logging.basicConfig(
        level=getattr(logging, logging_config["level"]),
        format=logging_config["format"],
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(logging_config["file"])
        ]
    )
    
    # 创建 FastAPI 应用
    app = FastAPI(
        title="TikClip API",
        description="API de cortes de vídeo com IA",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc"
    )
    
    # 设置应用状态
    app.state.mode = mode
    
    # 来源守卫在内、CORS 在外：预检由 CORS 应答，其他网页的写请求由守卫拦下。
    # 以前是 allow_origins=["*"] + allow_credentials，任意网页都能读设置里的 API Key。
    origins = allowed_origins()
    app.add_middleware(LocalOriginGuard, origins=origins, enforce_local_host=(mode == "desktop"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # 注册全局异常处理器
    app.add_exception_handler(Exception, global_exception_handler)
    
    # 启动事件
    @app.on_event("startup")
    async def startup_event():
        logger.info(f"Iniciando o serviço da API do TikClip (modo: {mode})...")
        
        # 导入所有模型以确保表被创建
        from backend.models.bilibili import BilibiliAccount, UploadRecord
        Base.metadata.create_all(bind=engine)
        logger.info("Tabelas do banco criadas")
        try:
            from backend.core.project_enum_migration import normalize_legacy_project_enums
            changed = normalize_legacy_project_enums(engine)
            if changed:
                logger.info("%s registro(s) de projeto tiveram valores antigos reescritos para os nomes reconhecidos nesta versão", changed)
        except Exception:
            logger.exception("Falha ao limpar valores antigos de enum; a listagem usará valores de fallback")
        try:
            # 上次进程被结束时还在跑的 Studio 任务不会再继续：标为失败并写明原因（RC156 #13）
            from backend.services.studio.restart_recovery import reconcile_interrupted_projects
            reconcile_interrupted_projects()
        except Exception:
            logger.exception("Falha ao recuperar o Studio após reinício; ao abrir o projeto haverá nova tentativa")
        try:
            # 只清 projects 根下、库里没有对应行的残留目录；库读失败则什么都不删（RC156 PR4）
            from backend.services.project_orphan_cleanup import cleanup_orphan_project_directories
            cleanup_orphan_project_directories()
        except Exception:
            logger.exception("Falha ao limpar pastas de projeto sem registro no banco")
        
        # 加载 API 密钥到环境变量
        api_key = get_api_key()
        if api_key:
            os.environ["DASHSCOPE_API_KEY"] = api_key
            logger.info("Chaves de API carregadas nas variáveis de ambiente")
        else:
            logger.warning("Nenhuma configuração de chave de API encontrada")
        
        # 根据模式进行不同的初始化
        if mode == "desktop":
            logger.info("Modo desktop: fila local e SQLite")
        else:
            logger.info("Modo web: Redis/Celery")
        
        logger.info("Gateway WebSocket desativado; usando o novo sistema simplificado de progresso")
    
    # 关闭事件
    @app.on_event("shutdown")
    async def shutdown_event():
        logger.info("Encerrando o serviço da API do TikClip...")
        logger.info("Gateway WebSocket desativado")
    
    # 注册路由
    app.include_router(health_router, prefix="/api/health", tags=["health"])
    app.include_router(api_router, prefix="/api/v1")
    
    # 添加 video-categories 路由（统一到 api_router 中）
    @app.get("/api/v1/video-categories")
    async def get_video_categories():
        """获取视频分类配置."""
        return {
            "categories": [
                {
                    "value": "default",
                    "name": "Padrão",
                    "description": "Processamento genérico de vídeo",
                    "icon": "🎬",
                    "color": "#4facfe"
                },
                {
                    "value": "knowledge",
                    "name": "Conhecimento e ciência",
                    "description": "Conteúdo de ciência, tecnologia, história e cultura",
                    "icon": "📚",
                    "color": "#52c41a"
                },
                {
                    "value": "entertainment",
                    "name": "Entretenimento",
                    "description": "Jogos, música, cinema e afins",
                    "icon": "🎮",
                    "color": "#722ed1"
                },
                {
                    "value": "business",
                    "name": "Negócios",
                    "description": "Negócios, empreendedorismo e investimentos",
                    "icon": "💼",
                    "color": "#fa8c16"
                },
                {
                    "value": "experience",
                    "name": "Relato de experiência",
                    "description": "Histórias pessoais e aprendizados de vida",
                    "icon": "🌟",
                    "color": "#eb2f96"
                },
                {
                    "value": "opinion",
                    "name": "Opinião e comentário",
                    "description": "Comentário de atualidades e análise de opinião",
                    "icon": "💭",
                    "color": "#13c2c2"
                },
                {
                    "value": "speech",
                    "name": "Palestra",
                    "description": "Palestras e apresentações públicas",
                    "icon": "🎤",
                    "color": "#f5222d"
                }
            ],
            "default_category": "default"
        }
    
    # 根健康检查
    @app.get("/health")
    async def root_health():
        try:
            return {
                "status": "ok",
                "mode": mode,
                "version": __version__
            }
        except Exception as e:
            return JSONResponse(
                status_code=500, 
                content={"status": "error", "detail": str(e)}
            )
    
    return app
