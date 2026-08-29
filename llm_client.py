"""llm_client.py —— OpenAI 兼容大模型 API 的异步封装。

仅封装 OpenAI 兼容接口（DeepSeek、Ollama、中转站等均适用）。
兼容性边界（见 doc/设计.md §九）：
  - Claude（Anthropic 官方 API）非 OpenAI 兼容，须经中转站暴露为 OpenAI 兼容端点；
  - DeepSeek-VL 为开源模型，须自建 vLLM/SGLang 提供 `.../v1` 端点；
  - Ollama 的 OpenAI 兼容端口为 `http://<host>:11434/v1`。
"""

import asyncio
import base64
import io
import time

import httpx
from PIL import Image
from openai import APIError, APIStatusError, AsyncOpenAI


class FatalAPIError(RuntimeError):
    """不可重试的致命错误（如 API Key 无效 / 无权限 / 多次重试仍失败），用于中止整批任务。"""


DEFAULT_SYSTEM_PROMPT = (
    "You are an image captioning assistant. "
    "Generate 5-10 comma-separated tags (danbooru style, lowercase) for the image. "
    "Output only the tags."
)


class LLMClient:
    """OpenAI 兼容视觉模型客户端：支持重试、请求超时、Token 累计统计。"""

    MAX_ATTEMPTS = 3  # 瞬时错误（429/5xx/网络）最多重试次数

    def __init__(self, api_key: str, base_url: str, model_name: str) -> None:
        self.model = model_name
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=httpx.Timeout(90.0, connect=10.0),
            max_retries=0,  # 重试逻辑由本类自行控制
        )
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @staticmethod
    def encode_image(image: Image.Image, max_side: int = 1280, quality: int = 80) -> str:
        """发送预处理：缩放 + JPEG 压缩，返回可直传 API 的 data URL。"""
        im = image.copy()
        im.thumbnail((max_side, max_side))
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    async def ping(self) -> tuple[bool, str]:
        """API 连通性测试：验证地址 / Key / 模型是否可用（几乎不消耗生成 token）。

        :return: (True, 详情) 或抛 FatalAPIError
        """
        t0 = time.perf_counter()
        detail: list[str] = []
        try:
            try:
                ids = [m.id for m in (await self.client.models.list()).data]
                detail.append(f"模型列表 {len(ids)} 个")
            except APIStatusError as e:
                if e.status_code in (401, 403):
                    raise FatalAPIError(f"API Key 无效或无权限（HTTP {e.status_code}）") from e
                if e.status_code == 404:
                    detail.append("/models 不可用（部分网关不支持）")
                else:
                    detail.append(f"/models 返回 HTTP {e.status_code}")
            except APIError as e:
                detail.append(f"/models 失败：{e}")
            # 最小文本补全，验证配置的模型真实可用
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            ok = bool(resp.choices and resp.choices[0].message.content is not None)
            latency = round((time.perf_counter() - t0) * 1000, 1)
            detail.append(f"模型 {self.model} {'可用' if ok else '返回空'}，耗时 {latency}ms")
            return True, "；".join(detail)
        except APIStatusError as e:
            if e.status_code in (401, 403):
                raise FatalAPIError(f"API Key 无效或无权限（HTTP {e.status_code}）") from e
            raise FatalAPIError(f"调用失败 HTTP {e.status_code}：{e.message}") from e
        except APIError as e:
            raise FatalAPIError(f"连接失败：{e}") from e

    async def generate(self, image_base64: str, prompt: str) -> str:
        """调用视觉模型生成标签文本。

        :param image_base64: 预处理后的图片 data URL
        :param prompt: system prompt（为空时使用内建默认）
        :raises FatalAPIError: Key 无效 / 无权限 / 多次重试后仍失败
        :return: 生成的标签文本（已 strip）
        """
        system = prompt.strip() if prompt and prompt.strip() else DEFAULT_SYSTEM_PROMPT
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请为这张图片生成标签，只输出结果，不要任何解释。"},
                    {"type": "image_url", "image_url": {"url": image_base64}},
                ],
            },
        ]

        last_error: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                resp = await self.client.chat.completions.create(model=self.model, messages=messages)
            except APIStatusError as e:
                # 401/403 = Key 无效或无权限 -> 致命，立即中止整批
                if e.status_code in (401, 403):
                    raise FatalAPIError(f"API Key 无效或无权限（HTTP {e.status_code}）") from e
                # 429 / 5xx 等瞬时错误 -> 指数退避后重试
                if attempt + 1 < self.MAX_ATTEMPTS:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise FatalAPIError(f"API 返回错误 HTTP {e.status_code}：{e.message}") from e
            except (APIError, OSError) as e:
                # 连接错误 / 超时 / 其它 openai 异常
                last_error = e
                if attempt + 1 < self.MAX_ATTEMPTS:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break
            except Exception as e:  # 兜底任何未知异常
                last_error = e
                if attempt + 1 < self.MAX_ATTEMPTS:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break

            content = resp.choices[0].message.content
            if resp.usage is not None:
                # usage 可能为 None（如 Ollama / 部分中转站），须判空
                self.total_prompt_tokens += resp.usage.prompt_tokens or 0
                self.total_completion_tokens += resp.usage.completion_tokens or 0
            return (content or "").strip()

        raise FatalAPIError(f"请求失败（重试 {self.MAX_ATTEMPTS} 次后）：{last_error}")
