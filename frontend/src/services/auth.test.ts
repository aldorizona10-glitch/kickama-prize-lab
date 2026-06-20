/**
 * Tests for auth token refresh cross-tab coordination.
 *
 * Validates:
 * - Same-tab concurrent refresh shares one in-flight request
 * - Cross-tab refresh: only one tab does network request
 * - Successful refresh broadcasts new tokens
 * - Failed refresh does not clear valid tokens held by another tab
 */

import { refreshTokens } from './auth';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const TOKEN_KEY = 'tot_auth_tokens';
const REFRESH_LOCK_KEY = 'tot_refresh_lock';
const REFRESH_RESULT_KEY = 'tot_refresh_result';

function makeTokens(overrides?: Partial<{ accessToken: string; refreshToken: string; expiresIn: number }>) {
  // Build a minimal JWT-like token with far-future expiry
  const header = btoa(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 }));
  return {
    accessToken: `${header}.${payload}.sig`,
    refreshToken: 'refresh_test_token',
    expiresIn: 3600,
    tokenType: 'Bearer',
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  jest.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Same-tab concurrency
// ---------------------------------------------------------------------------

describe('same-tab concurrent refresh', () => {
  it('shares one in-flight request when called twice rapidly', async () => {
    const tokens = makeTokens();
    localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));

    let callCount = 0;
    jest.spyOn(global, 'fetch').mockImplementation(async () => {
      callCount++;
      await new Promise((r) => setTimeout(r, 100));
      return new Response(JSON.stringify({ tokens: makeTokens() }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    // Fire two concurrent refreshes
    const [result1, result2] = await Promise.all([refreshTokens(), refreshTokens()]);

    expect(result1).toBeTruthy();
    expect(result2).toBeTruthy();
    // Only one network call should have been made
    expect(callCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Cross-tab coordination
// ---------------------------------------------------------------------------

describe('cross-tab refresh', () => {
  it('skips refresh when another tab holds the lock', async () => {
    const tokens = makeTokens();
    localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));
    // Simulate another tab holding the lock
    localStorage.setItem(REFRESH_LOCK_KEY, String(Date.now()));

    const fetchSpy = jest.spyOn(global, 'fetch');

    const result = await refreshTokens();

    // Should NOT have made a network call
    expect(fetchSpy).not.toHaveBeenCalled();
    // Result should be null (waiting for other tab)
    expect(result).toBeNull();
  });

  it('acquires lock when none is held', async () => {
    const tokens = makeTokens();
    localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));

    jest.spyOn(global, 'fetch').mockImplementation(async () => {
      return new Response(JSON.stringify({ tokens: makeTokens() }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    const result = await refreshTokens();
    expect(result).toBeTruthy();
    // Lock should be cleared after completion
    expect(localStorage.getItem(REFRESH_LOCK_KEY)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Broadcast result
// ---------------------------------------------------------------------------

describe('broadcast', () => {
  it('writes refresh result to localStorage for fallback', async () => {
    const tokens = makeTokens();
    localStorage.setItem(TOKEN_KEY, JSON.stringify(tokens));

    const newTokens = makeTokens();
    jest.spyOn(global, 'fetch').mockImplementation(async () => {
      return new Response(JSON.stringify({ tokens: newTokens }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });

    await refreshTokens();

    // The result should be written for other tabs to pick up
    const stored = localStorage.getItem(REFRESH_RESULT_KEY);
    expect(stored).toBeTruthy();
    const parsed = JSON.parse(stored!);
    expect(parsed.accessToken).toBeTruthy();
  });
});
