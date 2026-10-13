/**
  ******************************************************************************
  * @file    stm32f4xx_qspi.c
  * @brief   QUADSPI firmware library implementation.
  *
  * See the header's top-of-file comment: this is a from-scratch
  * reconstruction of the StdPeriph-style QUADSPI driver API that this
  * tree's src/fw/drivers/stm32f412/qspi.c calls, built directly against
  * the real QUADSPI register map and bit definitions from the official
  * STMicroelectronics/cmsis_device_f4 device header. It is not a
  * recovered original ST source file.
  ******************************************************************************
  */

#include "stm32f4xx_qspi.h"

void QSPI_StructInit(QSPI_InitTypeDef *QSPI_InitStruct) {
  QSPI_InitStruct->QSPI_SShift = QSPI_SShift_NoShift;
  QSPI_InitStruct->QSPI_Prescaler = 0;
  QSPI_InitStruct->QSPI_CKMode = QSPI_CKMode_Mode0;
  QSPI_InitStruct->QSPI_CSHTime = QSPI_CSHTime_1Cycle;
  QSPI_InitStruct->QSPI_FSize = 0;
  QSPI_InitStruct->QSPI_FSelect = QSPI_FSelect_1;
  QSPI_InitStruct->QSPI_DFlash = QSPI_DFlash_Disable;
}

void QSPI_Init(QSPI_InitTypeDef *QSPI_InitStruct) {
  uint32_t cr = QUADSPI->CR;
  cr &= ~(QUADSPI_CR_SSHIFT | QUADSPI_CR_PRESCALER | QUADSPI_CR_FSEL | QUADSPI_CR_DFM);
  cr |= QSPI_InitStruct->QSPI_SShift;
  cr |= ((QSPI_InitStruct->QSPI_Prescaler << QUADSPI_CR_PRESCALER_Pos) & QUADSPI_CR_PRESCALER);
  cr |= QSPI_InitStruct->QSPI_FSelect;
  cr |= QSPI_InitStruct->QSPI_DFlash;
  QUADSPI->CR = cr;

  uint32_t dcr = QUADSPI->DCR;
  dcr &= ~(QUADSPI_DCR_CKMODE | QUADSPI_DCR_CSHT | QUADSPI_DCR_FSIZE);
  dcr |= QSPI_InitStruct->QSPI_CKMode;
  dcr |= QSPI_InitStruct->QSPI_CSHTime;
  dcr |= ((QSPI_InitStruct->QSPI_FSize << QUADSPI_DCR_FSIZE_Pos) & QUADSPI_DCR_FSIZE);
  QUADSPI->DCR = dcr;
}

void QSPI_Cmd(FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    QUADSPI->CR |= QUADSPI_CR_EN;
  } else {
    QUADSPI->CR &= ~QUADSPI_CR_EN;
  }
}

void QSPI_SetDataLength(uint32_t length) {
  QUADSPI->DLR = length;
}

void QSPI_SetAddress(uint32_t address) {
  QUADSPI->AR = address;
}

void QSPI_SendData8(uint8_t data) {
  *(__IO uint8_t *)&QUADSPI->DR = data;
}

uint8_t QSPI_ReceiveData8(void) {
  return *(__IO uint8_t *)&QUADSPI->DR;
}

FlagStatus QSPI_GetFlagStatus(uint32_t QSPI_FLAG) {
  return ((QUADSPI->SR & QSPI_FLAG) != 0) ? SET : RESET;
}

void QSPI_ClearFlag(uint32_t QSPI_FLAG) {
  QUADSPI->FCR = QSPI_FLAG;
}

void QSPI_DMACmd(FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    QUADSPI->CR |= QUADSPI_CR_DMAEN;
  } else {
    QUADSPI->CR &= ~QUADSPI_CR_DMAEN;
  }
}

void QSPI_SetFIFOThreshold(uint32_t threshold_bytes) {
  uint32_t cr = QUADSPI->CR;
  cr &= ~QUADSPI_CR_FTHRES;
  // FTHRES field holds (threshold - 1), matching the real register's
  // documented "number of bytes - 1" encoding.
  uint32_t thresh_minus_1 = (threshold_bytes > 0) ? (threshold_bytes - 1) : 0;
  cr |= ((thresh_minus_1 << QUADSPI_CR_FTHRES_Pos) & QUADSPI_CR_FTHRES);
  QUADSPI->CR = cr;
}

void QSPI_AutoPollingMode_SetInterval(uint16_t interval) {
  QUADSPI->PIR = interval;
}

void QSPI_AutoPollingMode_Config(uint32_t match, uint32_t mask, uint32_t match_mode) {
  QUADSPI->PSMAR = match;
  QUADSPI->PSMKR = mask;
  uint32_t cr = QUADSPI->CR;
  cr &= ~QUADSPI_CR_PMM;
  cr |= match_mode;
  QUADSPI->CR = cr;
}

void QSPI_AutoPollingModeStopCmd(FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    QUADSPI->CR |= QUADSPI_CR_APMS;
  } else {
    QUADSPI->CR &= ~QUADSPI_CR_APMS;
  }
}

void QSPI_MemoryMappedMode_SetTimeout(uint16_t timeout_cycles) {
  QUADSPI->LPTR = timeout_cycles;
  QUADSPI->CR |= QUADSPI_CR_TCEN;
}

void QSPI_AbortRequest(void) {
  QUADSPI->CR |= QUADSPI_CR_ABORT;
}
