// This file prefers the generated config if present.
try {
  module.exports = require('./config.generated');
} catch (e) {
  module.exports = {
    SUBSCRIBE_TEMPLATE_ID: '',
    USE_CLOUD: false,
    CLOUD_ENV_ID: 'mindmate-miniprogram-d5abb807de8',
    CLOUD_SERVICE_NAME: 'mindmate-miniprogram',
    DIRECT_API_BASE: 'http://localhost:8000',
  };
}
