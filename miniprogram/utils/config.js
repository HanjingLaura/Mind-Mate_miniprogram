// This file prefers the generated config if present.
try {
  module.exports = require('./config.generated');
} catch (e) {
  module.exports = {
    SUBSCRIBE_TEMPLATE_ID: '',
    CLOUD_ENV_ID: 'mindmate-miniprogram-d5abb807de8',
    CLOUD_SERVICE_NAME: 'mindmate-miniprogram',
    DIRECT_API_BASE: '',
  };
}