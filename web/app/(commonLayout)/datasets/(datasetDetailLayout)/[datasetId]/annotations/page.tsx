import * as React from 'react'
import AnnotationPage from '@/app/components/datasets/annotations'

type Props = Readonly<{
  params: Promise<{ datasetId: string }>
}>

const DatasetAnnotations = async (props: Props) => {
  const params = await props.params

  const { datasetId } = params

  return <AnnotationPage datasetId={datasetId} />
}

export default DatasetAnnotations
