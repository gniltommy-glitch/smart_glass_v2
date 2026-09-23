/* STM32F411E-DISCO / MB1115: HSE 8 MHz, UART 1 Mbaud, âm thanh CS43L22.
 * ToF được nối trực tiếp Pi. PCM mono 16 kHz/16-bit được nhân đôi ra hai tai.
 * Chỉ main tạo gói UART; ISR RX/TX và DMA không thực hiện công việc chờ lâu.
 */
#include "stm32f4xx_hal.h"
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#define CODEC_ADDR (0x4AU << 1)
#define UART_BAUD 1000000U
#define UART_RING 4096U
#define PCM_RING 8192U
#define DMA_HALF_FRAMES 256U
#define MAX_PAYLOAD 1024U
#define FRAME_OVERHEAD 9U
#define I2C_TIMEOUT 20U
enum { MSG_START=0x10, MSG_PCM=0x11, MSG_STOP=0x12, MSG_BUTTON_ACK=0x20,
       MSG_ACK=0x80, MSG_BUTTON=0x81, MSG_STATUS=0x83, MSG_DONE=0x84, MSG_BOOT=0x85 };
enum { ACK_OK=0, ACK_BUSY=1, ACK_BAD=2, ACK_NO_AUDIO=3 };
static I2C_HandleTypeDef i2c1;
static I2S_HandleTypeDef i2s3;
static DMA_HandleTypeDef audio_dma;
static uint8_t rx_ring[UART_RING],tx_ring[UART_RING];
static volatile uint32_t rx_head,rx_tail,tx_head,tx_tail;
static volatile uint32_t uart_errors,rx_overflows;
static volatile bool rx_broken;
static int16_t pcm[PCM_RING];
static uint16_t dma_samples[DMA_HALF_FRAMES*4U];
static volatile uint32_t pcm_head,pcm_tail,samples_played,underruns;
static volatile uint32_t samples_received,samples_total,audio_done_tick;
static volatile bool audio_active,audio_playing,audio_draining;
static bool audio_ok;
static volatile bool dma_fault;
static volatile uint32_t exti_edges;
static uint32_t button_count,button_last_tx;
static uint32_t play_id,last_audio_command;
typedef struct { uint32_t count,created; uint8_t kind; } ButtonEvent;
static ButtonEvent buttons[16];
static uint8_t button_head,button_tail;
static uint32_t crc_errors;
static bool due(uint32_t now,uint32_t deadline) { return (int32_t)(now-deadline)>=0; }
static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0]|(uint16_t)p[1]<<8); }
static uint32_t le32(const uint8_t *p) { return (uint32_t)le16(p)|(uint32_t)le16(p+2)<<16; }
static void put16(uint8_t *p,uint16_t v) { p[0]=(uint8_t)v; p[1]=(uint8_t)(v>>8); }
static void put32(uint8_t *p,uint32_t v) { put16(p,(uint16_t)v); put16(p+2,(uint16_t)(v>>16)); }
static uint16_t crc16(const uint8_t *p,uint32_t n) {
    uint16_t crc=0xFFFF;
    while(n--) { crc^=(uint16_t)*p++<<8;
        for(unsigned i=0;i<8;i++) crc=(crc&0x8000)?(uint16_t)((crc<<1)^0x1021):(uint16_t)(crc<<1);
    }
    return crc;
}
static uint32_t tx_free(void) { return UART_RING-1U-(tx_head-tx_tail); }

static bool packet_send(uint8_t type,uint16_t seq,const uint8_t *p,uint16_t n) {
    uint8_t f[MAX_PAYLOAD+FRAME_OVERHEAD];
    if(n>MAX_PAYLOAD||tx_free()<n+FRAME_OVERHEAD) return false;
    f[0]=0xA5; f[1]=0x5A; f[2]=type; put16(f+3,seq); put16(f+5,n);
    if(n) memcpy(f+7,p,n);
    put16(f+7+n,crc16(f+2,n+5U));
    uint32_t h=tx_head;
    for(uint32_t i=0;i<n+FRAME_OVERHEAD;i++) tx_ring[(h+i)&(UART_RING-1U)]=f[i];
    __DMB(); tx_head=h+n+FRAME_OVERHEAD;
    uint32_t key=__get_PRIMASK(); __disable_irq(); USART2->CR1|=USART_CR1_TXEIE; __set_PRIMASK(key);
    return true;
}
void USART2_IRQHandler(void) {
    uint32_t sr=USART2->SR;
    if(sr&(USART_SR_RXNE|USART_SR_ORE|USART_SR_FE|USART_SR_NE|USART_SR_PE)) {
        uint8_t b=(uint8_t)USART2->DR;
        if(sr&(USART_SR_ORE|USART_SR_FE|USART_SR_NE|USART_SR_PE)) { uart_errors++; rx_broken=true; }
        else if(sr&USART_SR_RXNE) {
            if(rx_head-rx_tail<UART_RING-1U) {
                rx_ring[rx_head&(UART_RING-1U)]=b; __DMB(); rx_head++;
            } else { rx_overflows++; rx_broken=true; }
        }
    }
    if((sr&USART_SR_TXE)&&(USART2->CR1&USART_CR1_TXEIE)) {
        if(tx_tail!=tx_head) USART2->DR=tx_ring[tx_tail++&(UART_RING-1U)];
        else USART2->CR1&=~USART_CR1_TXEIE;
    }
}
void SysTick_Handler(void) { HAL_IncTick(); }
void EXTI0_IRQHandler(void) {
    if(EXTI->PR&1U) { EXTI->PR=1U; exti_edges++; } 
}
void DMA1_Stream5_IRQHandler(void) { HAL_DMA_IRQHandler(&audio_dma); }

/* DMA lấy PCM từ ring; thiếu dữ liệu thì phát mẫu 0 và đếm underrun. */
static void fill_audio(uint32_t offset) {
    uint32_t t=pcm_tail,available=pcm_head-t;
    if(audio_active&&!audio_playing&&!audio_draining&&
       (available>=1024U||(available&&samples_received==samples_total))) audio_playing=true;
    for(uint32_t i=0;i<DMA_HALF_FRAMES;i++) {
        int16_t sample=0;
        if(audio_playing) {
            if(t!=pcm_head) {
                sample=pcm[t&(PCM_RING-1U)]; t++; samples_played++;
                if(samples_played==samples_total) {
                    audio_playing=false; audio_draining=true; audio_done_tick=HAL_GetTick();
                }
            } else { audio_playing=false; underruns++; }
        }
        dma_samples[offset+2U*i]=(uint16_t)sample;
        dma_samples[offset+2U*i+1U]=(uint16_t)sample;
    }
    __DMB(); pcm_tail=t;
}
void HAL_I2S_TxHalfCpltCallback(I2S_HandleTypeDef *h) { if(h==&i2s3) fill_audio(0); }
void HAL_I2S_TxCpltCallback(I2S_HandleTypeDef *h) { if(h==&i2s3) fill_audio(DMA_HALF_FRAMES*2U); }
void HAL_I2S_ErrorCallback(I2S_HandleTypeDef *h) { if(h==&i2s3) dma_fault=true; }
static void gpio_af(GPIO_TypeDef *port,uint32_t pins,uint32_t af,bool od) {
    GPIO_InitTypeDef g={0}; g.Pin=pins; g.Mode=od?GPIO_MODE_AF_OD:GPIO_MODE_AF_PP;
    g.Pull=GPIO_NOPULL; g.Speed=GPIO_SPEED_FREQ_VERY_HIGH; g.Alternate=af; HAL_GPIO_Init(port,&g);
}
static void gpio_init(void) {
    __HAL_RCC_GPIOA_CLK_ENABLE(); __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE(); __HAL_RCC_GPIOD_CLK_ENABLE(); __HAL_RCC_SYSCFG_CLK_ENABLE();
    GPIO_InitTypeDef g={0};
    g.Pin=GPIO_PIN_12|GPIO_PIN_13|GPIO_PIN_14|GPIO_PIN_15|GPIO_PIN_4;
    g.Mode=GPIO_MODE_OUTPUT_PP; g.Speed=GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOD,&g); GPIOD->BSRR=g.Pin<<16;
    g.Pin=GPIO_PIN_0; g.Mode=GPIO_MODE_INPUT; g.Pull=GPIO_PULLDOWN; HAL_GPIO_Init(GPIOA,&g);
    SYSCFG->EXTICR[0]&=~0xFU; EXTI->IMR&=~1U;
    EXTI->FTSR|=1U; EXTI->RTSR|=1U; EXTI->PR=1U;
    HAL_NVIC_SetPriority(EXTI0_IRQn,3,0); HAL_NVIC_ClearPendingIRQ(EXTI0_IRQn);
    HAL_NVIC_EnableIRQ(EXTI0_IRQn); EXTI->IMR|=1U;
    gpio_af(GPIOA,GPIO_PIN_2|GPIO_PIN_3,GPIO_AF7_USART2,false);
    gpio_af(GPIOB,GPIO_PIN_6|GPIO_PIN_9,GPIO_AF4_I2C1,true);
    gpio_af(GPIOA,GPIO_PIN_4,GPIO_AF6_SPI3,false);
    gpio_af(GPIOC,GPIO_PIN_7|GPIO_PIN_10|GPIO_PIN_12,GPIO_AF6_SPI3,false);
}
static bool clock_init(void) {
    __HAL_RCC_PWR_CLK_ENABLE(); __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
    RCC_OscInitTypeDef o={0};
    o.OscillatorType=RCC_OSCILLATORTYPE_HSE; o.HSEState=RCC_HSE_ON;
    o.PLL.PLLState=RCC_PLL_ON; o.PLL.PLLSource=RCC_PLLSOURCE_HSE;
    o.PLL.PLLM=8; o.PLL.PLLN=192; o.PLL.PLLP=RCC_PLLP_DIV2; o.PLL.PLLQ=4;
    if(HAL_RCC_OscConfig(&o)!=HAL_OK) return false;
    RCC_ClkInitTypeDef c={0};
    c.ClockType=RCC_CLOCKTYPE_SYSCLK|RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
    c.SYSCLKSource=RCC_SYSCLKSOURCE_PLLCLK; c.AHBCLKDivider=RCC_SYSCLK_DIV1;
    c.APB1CLKDivider=RCC_HCLK_DIV2; c.APB2CLKDivider=RCC_HCLK_DIV1;
    if(HAL_RCC_ClockConfig(&c,FLASH_LATENCY_3)!=HAL_OK) return false;
    RCC_PeriphCLKInitTypeDef a={0}; a.PeriphClockSelection=RCC_PERIPHCLK_I2S;
    a.PLLI2S.PLLI2SM=8; a.PLLI2S.PLLI2SN=213; a.PLLI2S.PLLI2SR=2;
    return HAL_RCCEx_PeriphCLKConfig(&a)==HAL_OK;
}
/* RX UART ưu tiên cao hơn DMA để nhận được mỗi byte cách nhau 10 micro giây. */
static void uart_init(void) {
    __HAL_RCC_USART2_CLK_ENABLE(); USART2->CR1=0; USART2->CR2=0; USART2->CR3=USART_CR3_EIE;
    USART2->BRR=(HAL_RCC_GetPCLK1Freq()+UART_BAUD/2U)/UART_BAUD;
    USART2->CR1=USART_CR1_UE|USART_CR1_TE|USART_CR1_RE|USART_CR1_RXNEIE;
    
    HAL_NVIC_SetPriority(USART2_IRQn,1,0); HAL_NVIC_EnableIRQ(USART2_IRQn);
}
static bool i2c_init(I2C_HandleTypeDef *h,I2C_TypeDef *inst) {
    h->Instance=inst; h->Init.ClockSpeed=100000; h->Init.DutyCycle=I2C_DUTYCYCLE_2;
    h->Init.OwnAddress1=0; h->Init.AddressingMode=I2C_ADDRESSINGMODE_7BIT;
    h->Init.DualAddressMode=I2C_DUALADDRESS_DISABLE; h->Init.GeneralCallMode=I2C_GENERALCALL_DISABLE;
    h->Init.NoStretchMode=I2C_NOSTRETCH_DISABLE;
    return HAL_I2C_Init(h)==HAL_OK;
}
static bool codec_write(uint8_t reg,uint8_t value) {
    return HAL_I2C_Mem_Write(&i2c1,CODEC_ADDR,reg,I2C_MEMADD_SIZE_8BIT,&value,1,I2C_TIMEOUT)==HAL_OK;
}
static bool codec_read(uint8_t reg,uint8_t *value) {
    return HAL_I2C_Mem_Read(&i2c1,CODEC_ADDR,reg,I2C_MEMADD_SIZE_8BIT,value,1,I2C_TIMEOUT)==HAL_OK;
}
/* I2C1 PB6/PB9 cấu hình codec; I2S3 + DMA1 Stream5 truyền liên tục.
 * Reset PD4, đọc ID, trình tự khởi tạo, headphone bật/speaker tắt, volume -20 dB.
 */
static bool audio_init(void) {
    uint8_t id=0,v=0;
    GPIOD->BSRR=GPIO_PIN_4<<16; HAL_Delay(2); GPIOD->BSRR=GPIO_PIN_4; HAL_Delay(2);
    if(!codec_read(0x01,&id)||(id&0xF8)!=0xE0) return false;
    if(!codec_write(0x02,0x01)||!codec_write(0x00,0x99)||!codec_write(0x47,0x80)||
       !codec_read(0x32,&v)||!codec_write(0x32,v|0x80)||!codec_write(0x32,v&0x7F)||
       !codec_write(0x00,0x00)) return false;
    
    if(!codec_write(0x04,0xAF)||!codec_write(0x05,0x81)||!codec_write(0x06,0x07)||
       !codec_write(0x0A,0x00)||!codec_write(0x0D,0x00)||
       !codec_write(0x20,0xD8)||!codec_write(0x21,0xD8)||
       !codec_write(0x22,0x00)||!codec_write(0x23,0x00)) return false;
    __HAL_RCC_SPI3_CLK_ENABLE(); __HAL_RCC_DMA1_CLK_ENABLE();
    i2s3.Instance=SPI3; i2s3.Init.Mode=I2S_MODE_MASTER_TX; i2s3.Init.Standard=I2S_STANDARD_PHILIPS;
    i2s3.Init.DataFormat=I2S_DATAFORMAT_16B; i2s3.Init.MCLKOutput=I2S_MCLKOUTPUT_ENABLE;
    i2s3.Init.AudioFreq=I2S_AUDIOFREQ_16K; i2s3.Init.CPOL=I2S_CPOL_LOW;
    i2s3.Init.ClockSource=I2S_CLOCK_PLL; i2s3.Init.FullDuplexMode=I2S_FULLDUPLEXMODE_DISABLE;
    if(HAL_I2S_Init(&i2s3)!=HAL_OK) return false;
    audio_dma.Instance=DMA1_Stream5; audio_dma.Init.Channel=DMA_CHANNEL_0;
    audio_dma.Init.Direction=DMA_MEMORY_TO_PERIPH; audio_dma.Init.PeriphInc=DMA_PINC_DISABLE;
    audio_dma.Init.MemInc=DMA_MINC_ENABLE; audio_dma.Init.PeriphDataAlignment=DMA_PDATAALIGN_HALFWORD;
    audio_dma.Init.MemDataAlignment=DMA_MDATAALIGN_HALFWORD; audio_dma.Init.Mode=DMA_CIRCULAR;
    audio_dma.Init.Priority=DMA_PRIORITY_HIGH; audio_dma.Init.FIFOMode=DMA_FIFOMODE_DISABLE;
    if(HAL_DMA_Init(&audio_dma)!=HAL_OK) return false;
    __HAL_LINKDMA(&i2s3,hdmatx,audio_dma);
    HAL_NVIC_SetPriority(DMA1_Stream5_IRQn,2,0); HAL_NVIC_EnableIRQ(DMA1_Stream5_IRQn);
    if(HAL_I2S_Transmit_DMA(&i2s3,dma_samples,DMA_HALF_FRAMES*4U)!=HAL_OK) return false;
    HAL_Delay(2);
    return codec_write(0x02,0x9E);
}

/* Giữ DMA chạy với mẫu 0 khi rảnh; không ghi vào nửa bộ đệm DMA đang phát. */
static void audio_reset(uint32_t total) {
    uint32_t key=__get_PRIMASK(); __disable_irq();
    audio_playing=false; audio_active=false; audio_draining=false;
    pcm_head=pcm_tail=0; samples_played=0; samples_received=0; samples_total=total;
    if(!total) play_id=0;
    audio_active=total!=0; __DMB(); __set_PRIMASK(key);
    
}
static void command_handle(uint8_t type,uint16_t seq,const uint8_t *p,uint16_t n) {
    static bool have_last;
    static uint16_t last_seq,last_crc;
    static uint8_t last_type;
    uint8_t ack=ACK_OK;
    if(type==MSG_BUTTON_ACK) {
        if(n==4 && button_tail!=button_head && buttons[button_tail].count==le32(p))
            button_tail=(uint8_t)((button_tail+1U)%16U);
        return;
    }
    uint16_t digest=crc16(p,n);
    if(have_last&&seq==last_seq&&type==last_type&&digest==last_crc) {
        (void)packet_send(MSG_ACK,seq,&ack,1); return;
    }
    if(type==MSG_STOP&&n==0) audio_reset(0);
    else if(!audio_ok) ack=ACK_NO_AUDIO;
    else if(type==MSG_START&&n==8&&le32(p)>0&&le32(p+4)>0) {
        if(audio_active) ack=ACK_BUSY;
        else { play_id=le32(p); audio_reset(le32(p+4)); }
    } else if(type==MSG_PCM&&n&&!(n&1U)&&audio_active) {
        uint32_t count=n/2U;
        if(count>samples_total-samples_received) ack=ACK_BAD;
        else if(count>PCM_RING-(pcm_head-pcm_tail)) ack=ACK_BUSY;
        else {
            uint32_t h=pcm_head;
            for(uint32_t i=0;i<count;i++) pcm[(h+i)&(PCM_RING-1U)]=(int16_t)le16(p+2U*i);
            uint32_t key=__get_PRIMASK(); __disable_irq();
            samples_received+=count; __DMB(); pcm_head=h+count; __set_PRIMASK(key);
        }
    } else ack=ACK_BAD;
    if(ack==ACK_OK) { last_audio_command=HAL_GetTick(); have_last=true; last_seq=seq; last_type=type; last_crc=digest; }
    (void)packet_send(MSG_ACK,seq,&ack,1);
}
static void protocol_service(uint32_t now) {
    static uint8_t buf[MAX_PAYLOAD+FRAME_OVERHEAD];
    static uint16_t used;
    static uint32_t last_rx;
    if(rx_broken) {
        uint32_t key=__get_PRIMASK(); __disable_irq();
        rx_tail=rx_head; rx_broken=false; __set_PRIMASK(key); used=0;
    }
    if(used&&now-last_rx>100) used=0;
    unsigned budget=2048;
    while(rx_tail!=rx_head&&budget--&&tx_free()>=64U) {
        uint8_t b=rx_ring[rx_tail&(UART_RING-1U)]; __DMB(); rx_tail++;
        last_rx=now; buf[used++]=b;
        while(used) {
            if(buf[0]!=0xA5||(used>=2&&buf[1]!=0x5A)) { memmove(buf,buf+1,--used); continue; }
            if(used<7) break;
            uint16_t n=le16(buf+5);
            if(n>MAX_PAYLOAD) { memmove(buf,buf+1,--used); continue; }
            if(used<n+FRAME_OVERHEAD) break;
            if(crc16(buf+2,n+5U)==le16(buf+7+n)) {
                command_handle(buf[2],le16(buf+3),buf+7,n);
                uint16_t left=used-(n+FRAME_OVERHEAD);
                memmove(buf,buf+n+FRAME_OVERHEAD,left); used=left;
            } else { crc_errors++; memmove(buf,buf+1,--used); }
        }
    }
}
/* Nút PA0: chống dội 25 ms; phân loại khi thả; ngắt EXTI không chờ UART. */
static void button_service(uint32_t now) {
    static bool raw_last,stable,pressed_valid;
    static uint32_t changed,accepted_edge,pressed_at;
    bool raw=(GPIOA->IDR&1U)!=0;
    if(raw!=raw_last) { raw_last=raw; changed=now; }
    if(raw!=stable&&now-changed>=25U) {
        stable=raw;
        if(stable&&exti_edges!=accepted_edge) {
            accepted_edge=exti_edges; pressed_valid=true; pressed_at=now;
            GPIOD->BSRR=GPIO_PIN_14;
        } else if(!stable&&pressed_valid) {
            pressed_valid=false;
            uint8_t next=(uint8_t)((button_head+1U)%16U);
            if(next==button_tail) GPIOD->BSRR=GPIO_PIN_15;
            else {
                buttons[button_head]=(ButtonEvent){++button_count,now,(now-pressed_at>=1200U)?2U:1U};
                button_head=next; button_last_tx=now-100U;
            }
        }
    }
    if(button_tail!=button_head) {
        ButtonEvent *item=&buttons[button_tail];
        if(now-item->created>2000U) {
            button_tail=(uint8_t)((button_tail+1U)%16U);
        } else if(now-button_last_tx>=100U&&tx_free()>=64U) {
            uint8_t p[5]; put32(p,item->count); p[4]=item->kind;
            if(packet_send(MSG_BUTTON,(uint16_t)item->count,p,5)) button_last_tx=now;
        }
    }
}
/* DONE chỉ gửi sau khi mẫu cuối đã đi qua cả hai nửa DMA. */
static void status_service(uint32_t now) {
    static uint32_t last;
    if(dma_fault) { audio_ok=false; audio_reset(0); dma_fault=false; GPIOD->BSRR=GPIO_PIN_13; }
    /* Mất Pi/đường UART thì tự bỏ âm thanh dở sau 1,5 giây. */
    if(audio_active&&samples_received<samples_total&&now-last_audio_command>1500U) audio_reset(0);
    if(audio_draining&&due(now,audio_done_tick+34U)) {
        uint8_t p[8]; put32(p,play_id); put32(p+4,samples_played);
        if(packet_send(MSG_DONE,0,p,8)) { audio_active=false; audio_draining=false; }
    }
    if(now-last<1000U||tx_free()<128U) return;
    last=now; GPIOD->ODR^=GPIO_PIN_12;
    uint8_t p[40]={2,(uint8_t)audio_ok,(uint8_t)audio_active,0};
    put32(p+4,now); put32(p+8,underruns); put32(p+12,uart_errors); put32(p+16,rx_overflows);
    put32(p+20,crc_errors); put32(p+24,samples_played); put32(p+28,play_id);
    put32(p+32,button_count); put32(p+36,exti_edges);
    (void)packet_send(MSG_STATUS,0,p,sizeof(p));
}
int main(void) {
    HAL_Init(); gpio_init();
    if(!clock_init()) {
        uint32_t last=0;
        while(1) { uint32_t now=HAL_GetTick();
            if(now-last>=200U) { last=now; GPIOD->ODR^=GPIO_PIN_13; } }
    }
    uart_init(); (void)packet_send(MSG_BOOT,0,0,0);
    __HAL_RCC_I2C1_CLK_ENABLE();
    (void)i2c_init(&i2c1,I2C1);
    audio_ok=audio_init(); if(!audio_ok) GPIOD->BSRR=GPIO_PIN_13;
    while(1) {
        uint32_t now=HAL_GetTick();
        button_service(now); protocol_service(now); status_service(now);
    }
}
/* Startup GCC của bộ build độc lập cần các hàm khởi tạo này. */
void _init(void) {}
void _fini(void) {}
