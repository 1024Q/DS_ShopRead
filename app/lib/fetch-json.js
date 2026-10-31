// Shared JSON request helper: maps HTTP status to a readable Chinese message instead of leaking raw parser errors.
export const shopStateLabels = {
  sleeping: '采集休眠',
  stopped: '未启动',
  logging_in: '登录中',
  logged_in: '已登录',
  needs_verification: '等待验证',
  login_failed: '登录失败',
};

export function errorMessage(status, data) {
  if (data && typeof data.detail === 'string') return data.detail;
  if (data && typeof data.message === 'string' && data.message) return data.message;
  if (status === 401 || status === 403) return '连接已失效，请重新登录或检查接口连接';
  if (status === 404) return '请求的内容不存在或已被删除';
  if (status === 409) return '操作冲突，请刷新后重试';
  if (status === 400 || status === 422) return '请检查填写内容是否完整、长度是否超出限制';
  if (status === 503) return '服务正在更新或暂不可用，请稍后重试';
  if (status >= 500) return '服务暂时不可用，请稍后重试';
  return '请求失败，请稍后重试';
}

export async function fetchJson(url, options = {}) {
  let result;
  try {
    result = await fetch(url, { cache: 'no-store', ...options });
  } catch (e) {
    if (e?.name === 'AbortError') throw e;
    throw new Error('无法连接服务，请检查网络或服务是否已启动');
  }
  let data = null;
  const text = await result.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!result.ok) {
    const error = new Error(errorMessage(result.status, data));
    error.status = result.status;
    error.data = data;
    throw error;
  }
  if (data === null && text) throw new Error('服务返回了无法识别的内容，请稍后重试');
  return data;
}
