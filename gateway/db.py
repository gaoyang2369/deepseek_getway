from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def database(url):
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=15,
        max_overflow=15,
        pool_use_lifo=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)
