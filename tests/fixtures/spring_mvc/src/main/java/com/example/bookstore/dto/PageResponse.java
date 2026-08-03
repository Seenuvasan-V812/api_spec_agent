package com.example.bookstore.dto;

import java.util.List;

/** Generic pagination envelope. */
public class PageResponse<T> {
    private List<T> items;
    private long total;
    private int page;
    private int pageSize;
}
