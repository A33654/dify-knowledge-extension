// oxlint-disable-next-line no-restricted-imports -- The dataset annotation endpoints are not in the generated console contract yet, so this feature module owns the shared wrapper import.
import { del, get, patch, post } from './base'

export type DatasetAnnotationFeedbackType = 'like' | 'dislike'

export type DatasetAnnotation = {
  id: string
  question: string
  answer: string
  tags: string[]
  feedback_type: DatasetAnnotationFeedbackType | null
  created_by: string
  created_at: number | null
  updated_at: number | null
}

export type DatasetAnnotationListResponse = {
  data: DatasetAnnotation[]
  has_more: boolean
  limit: number
  total: number
  page: number
}

export type DatasetAnnotationListQuery = {
  page?: number
  limit?: number
  tag?: string
  feedback_type?: DatasetAnnotationFeedbackType
  start_time?: number
  end_time?: number
}

export type DatasetAnnotationCreatePayload = {
  question: string
  answer: string
  tags?: string[]
  feedback_type?: DatasetAnnotationFeedbackType | null
}

export type DatasetAnnotationUpdatePayload = Partial<DatasetAnnotationCreatePayload>

export const fetchDatasetAnnotations = (datasetId: string, query: DatasetAnnotationListQuery = {}) => {
  // oxlint-disable-next-line typescript/no-deprecated -- Dataset annotation APIs are not in the generated console contract yet.
  return get<DatasetAnnotationListResponse>(`/datasets/${datasetId}/annotations`, { params: query })
}

export const createDatasetAnnotation = (
  datasetId: string,
  payload: DatasetAnnotationCreatePayload,
) => {
  // oxlint-disable-next-line typescript/no-deprecated -- Dataset annotation APIs are not in the generated console contract yet.
  return post<DatasetAnnotation>(`/datasets/${datasetId}/annotations`, { body: payload })
}

export const updateDatasetAnnotation = (
  datasetId: string,
  annotationId: string,
  payload: DatasetAnnotationUpdatePayload,
) => {
  // oxlint-disable-next-line typescript/no-deprecated -- Dataset annotation APIs are not in the generated console contract yet.
  return patch<DatasetAnnotation>(`/datasets/${datasetId}/annotations/${annotationId}`, { body: payload })
}

export const deleteDatasetAnnotation = (datasetId: string, annotationId: string) => {
  // oxlint-disable-next-line typescript/no-deprecated -- Dataset annotation APIs are not in the generated console contract yet.
  return del<{ result: string }>(`/datasets/${datasetId}/annotations/${annotationId}`)
}

export const exportDatasetAnnotationsBlob = async (datasetId: string, type: 'sft' | 'eval') => {
  // oxlint-disable-next-line typescript/no-deprecated -- Dataset annotation export needs the raw response for a Blob download.
  const response = await get<Response>(
    `/datasets/${datasetId}/annotations/export?type=${type}`,
    {},
    { needAllResponseContent: true },
  )
  return response.blob()
}