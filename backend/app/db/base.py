"""SQLAlchemy 2.0 声明式基类。所有 ORM 模型都继承 Base。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全局 ORM 模型基类。

    所有具体的业务实体模型类（如知识库表、向量索引记录表等）均需继承此类。
    SQLAlchemy 会通过该基类的 metadata 统一收集并维护所有的表结构映射信息。
    """
    pass