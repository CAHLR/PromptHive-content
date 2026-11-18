import { OpenAI } from "langchain/llms/openai";
import { ChatOpenAI } from "@langchain/openai";

const llm = new OpenAI({ openAIApiKey: import.meta.env.VITE_OPENAI_API_KEY });
const llm_chat = new ChatOpenAI({
  openAIApiKey: import.meta.env.VITE_OPENAI_API_KEY,
  streaming: true,
  callbacks: [
    {
      handleLLMNewToken(token) {
        process.stdout.write(token);
      },
    },
  ],
});

export async function getAnswer(question) {
  let stream = "";
  try {
    stream = await llm_chat.stream(question);
    for await (const chunk of stream) {
      console.log(stream);
    }
  } catch (e) {
    console.log(e);
  }
  return stream["content"];
}
