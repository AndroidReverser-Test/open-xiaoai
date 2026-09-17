import { sleep } from "@mi-gpt/utils";
import { OpenXiaoAIConfig } from "./migpt/xiaoai.js";

export const kOpenXiaoAIConfig: OpenXiaoAIConfig = {
  openai: {
    /**
     * 大模型服务提供商的接口地址
     *
     * 默认使用 OpenCode Zen 的免费 DeepSeek V4 Flash 模型。
     * 也支持其他兼容 OpenAI 接口的大模型服务。
     *
     * 注意：一般以 /v1 结尾，不包含 /chat/completions 部分
     * - ✅ https://opencode.ai/zen/v1
     * - ❌ https://api.openai.com/v1/（最后多了一个 /
     * - ❌ https://api.openai.com/v1/chat/completions（不需要加 /chat/completions）
     */
    baseURL: "https://opencode.ai/zen/v1",
    /**
     * OpenCode Zen API 密钥。
     *
     * 当前 Zen 要求 API key，即使使用免费模型。通过 OPENCODE_API_KEY 环境变量
     * 提供，避免将密钥写入或提交到配置文件。
     */
    apiKey: process.env.OPENCODE_API_KEY ?? "",
    /**
     * 模型名称
     */
    model: "deepseek-v4-flash-free",
  },
  prompt: {
    /**
     * 系统提示词，如需关闭可设置为：''（空字符串）
     */
    system: "你是一个智能助手，请根据用户的问题给出回答。",
  },
  context: {
    /**
     * 每次对话携带的最大历史消息数（如需关闭可设置为：0）
     */
    historyMaxLength: 10,
  },
  /**
   * 空字符串会匹配每条最终语音识别结果，使音箱直接回复所有语音请求。
   * 如需仅处理特定前缀，可替换为例如：["请", "你"]。
   */
  callAIKeywords: [""],
  /**
   * 自定义消息回复
   */
  async onMessage(engine, { text }) {
    if (text === "测试播放文字") {
      return { text: "你好，很高兴认识你！" };
    }

    if (text === "测试播放音乐") {
      return { url: "https://example.com/hello.mp3" };
    }

    if (text === "测试其他能力") {
      // 打断原来小爱的回复
      await engine.speaker.abortXiaoAI();

      // 播放文字
      await sleep(2000); // 打断小爱后需要等待 2 秒，使其恢复运行后才能继续 TTS
      await engine.speaker.play({ text: "你好，很高兴认识你！", blocking: true });

      // 播放音频链接
      await engine.speaker.play({ url: "https://example.com/hello.mp3" });

      // 告诉 MiGPT 已经处理过这条消息了，不再使用默认的 AI 回复
      return { handled: true };
    }
  },
};
