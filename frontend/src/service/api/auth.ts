import { request } from '../request';

/**
 * Login —— 本后端走 fastapi-users 的 cookie 登录：
 * POST /auth/jwt/login（form-urlencoded: username/password），成功后 set httpOnly cookie。
 * 无 token 返回，这里回一个哨兵 token 让上层（基于 token 判断登录态）正常工作。
 */
export async function fetchLogin(userName: string, password: string) {
  const body = new URLSearchParams();
  body.set('username', userName);
  body.set('password', password);

  const res: any = await request({
    url: '/auth/jwt/login',
    method: 'post',
    data: body,
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
  });

  if (res.error) return res;
  return { data: { token: 'cookie', refreshToken: '' }, error: null };
}

/** Get user info —— GET /users/me，映射成 Soybean 的 UserInfo */
export async function fetchGetUserInfo() {
  const res: any = await request({ url: '/users/me' });
  if (res.error) return res;

  const u = res.data || {};
  const isSuper = Boolean(u.is_superuser);
  const info = {
    userId: String(u.id ?? ''),
    userName: u.username || u.email || '',
    email: u.email || '',
    isSuperuser: isSuper,
    roles: [isSuper ? 'R_SUPER' : 'R_USER'],
    buttons: []
  };
  return { data: info, error: null };
}

/** Logout —— POST /auth/jwt/logout，清 cookie */
export function fetchLogout() {
  return request({ url: '/auth/jwt/logout', method: 'post' });
}

/** cookie 模式无 token 刷新；保留空实现以兼容引用 */
export function fetchRefreshToken(_refreshToken: string) {
  return Promise.resolve({ data: { token: 'cookie', refreshToken: '' }, error: null });
}

export function fetchCustomBackendError(code: string, msg: string) {
  return request({ url: '/auth/error', params: { code, msg } });
}
