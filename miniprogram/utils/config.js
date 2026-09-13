// This file prefers the generated config if present.
try {
  module.exports = require('./config.generated')
} catch (e) {
  // 安全的本地开发默认值；正式上传前运行 scripts/generate-miniprogram-config.ps1。
  module.exports = {
    SUBSCRIBE_TEMPLATE_ID: '',
    USE_CLOUD: false,
    CLOUD_ENV_ID: 'prod-d1gf0x42k6f1d821b',
    CLOUD_SERVICE_NAME: 'django-rt60',
    DIRECT_API_BASE: 'http://localhost:8000',
  }
}
