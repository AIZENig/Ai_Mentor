import { z } from "zod";

export const generateVideoSchema = z.object({
  courseId: z.union([z.string(), z.number()]).transform((val) => Number(val)),
  lessonId: z.union([z.string(), z.number()]).transform((val) => Number(val)),
  celebrity: z.string().min(1, "Celebrity name is required"),
});


export const explainCodeSchema = z.object({
  code: z.string().trim().min(1, "Code is required").max(20000),
  language: z.string().trim().min(1, "Programming language is required").max(50),
  mode: z.enum(["explain", "debug", "optimize"]),
});