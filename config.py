import os

class Config:
    """Base config class"""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'you-will-never-guess'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///instance/app.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    RSSHUB_BASE_URL = os.environ.get('RSSHUB_BASE_URL') or 'http://localhost:1200'
    CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL') or 30)
    LOG_LEVEL = os.environ.get('LOG_LEVEL') or 'INFO'

class DevelopmentConfig(Config):
    """Development config"""
    DEBUG = True

class TestingConfig(Config):
    """Testing config"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///instance/test.db'

class ProductionConfig(Config):
    """Production config"""
    DEBUG = False
    TESTING = False
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'hard-to-guess-string'
    
    # Use PostgreSQL in production
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'postgresql://user:password@localhost/rsshub_admin'

# Config dictionary for app.config.from_object
config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}