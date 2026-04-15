from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="active")
    source = Column(String(100), nullable=False, default="jumpcloud")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Access(Base):
    __tablename__ = "access"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    tool = Column(String(100), nullable=False)
    role = Column(String(100), nullable=True)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "tool": self.tool,
            "role": self.role,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    performed_by = Column(String(255), nullable=False)
    action = Column(String(100), nullable=False)
    target_email = Column(String(255), nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "performed_by": self.performed_by,
            "action": self.action,
            "target_email": self.target_email,
            "details": self.details,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
