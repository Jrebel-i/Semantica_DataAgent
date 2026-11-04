// @vitest-environment jsdom
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App } from './App';

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, 'matchMedia', { writable: true, value: () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(window, 'getComputedStyle', { value: () => ({ getPropertyValue: () => '' }), writable: true });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

test('sign in and render dependency states', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/api/v1/me' ? new Response('{}', {status: 200}) : new Response(JSON.stringify({neo4j: {status:'up', elapsed_ms: 1}, milvus: {status:'down', elapsed_ms: 2}}), {status: 200})));
  render(<App />);
  fireEvent.change(screen.getByLabelText('粘贴个人令牌'), { target: { value: 'token' } });
  fireEvent.click(screen.getByRole('button', { name: /登\s*录/ }));
  expect(await screen.findByText('正常')).toBeTruthy();
  expect(screen.getByText('不可用')).toBeTruthy();
  expect(localStorage.getItem('token')).toBe('token');
});

test('401 returns to login', async () => {
  localStorage.setItem('token', 'bad');
  vi.stubGlobal('fetch', vi.fn(async () => new Response('', {status: 401})));
  render(<App />);
  expect(await screen.findByText('令牌无效')).toBeTruthy();
  await waitFor(() => expect(localStorage.getItem('token')).toBeNull());
  expect(screen.getByRole('button', { name: /登\s*录/ })).toBeTruthy();
});

test('language selection persists', () => {
  render(<App />);
  fireEvent.click(screen.getByText('English'));
  expect(screen.getByText('System status')).toBeTruthy();
  expect(localStorage.getItem('language')).toBe('en');
});
